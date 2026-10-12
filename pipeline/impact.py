"""주가 영향력(Price Impact) 점수 — 펀더멘털 영향 × 실제 주가 반응.

  score(0~100) = 100 × (0.6 × fundamental + 0.4 × reaction)
  fundamental  = 0.5 × core_business + 0.5 × pnl_effect   (LLM이 분석 JSON `impact`에 산출; 없으면 뉴스 유형·확신도로 추정)
  reaction     = 뉴스 전후 1~3분 주가 변동 절댓값(%)을 0~1로 사상: 0.3%→0.2, 1%→0.5, 2%→0.8, ≥3%→1.0
                 (장중: 발행 직전 1분봉 종가 대비 +1/+2/+3분 종가 변동과 고저 범위의 최대값.
                  장외: 직전 세션 종가 대비 다음 세션 09:01~09:03 종가. 아직 개장 전이면 '개장 대기'로 반응 미측정)
  반응이 미측정이면 score = 100 × fundamental 이고 `reaction.status`에 사유를 남긴다.

분봉 데이터: 키움 REST ka10080(주식분봉차트조회). 자격증명은 ~/.claude/mcp-servers/kiwoom-exec-mcp/credentials.env 의 APP_KEY/APP_SECRET/KIWOOM_MODE(폴백) 또는 환경변수.

사용:
  python -m pipeline.impact update            # impact.score 없는 분석 JSON 전부 계산·저장
  python -m pipeline.impact one <analysis.json> [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = ROOT / "data" / "analysis"
CACHE = ROOT / "data" / "cache"
CRED_FILES = [ROOT / ".env", Path.home() / ".claude" / "mcp-servers" / "kiwoom-exec-mcp" / "credentials.env"]
WEIGHT_FUND, WEIGHT_REACT = 0.6, 0.4
REACT_CURVE = [(0.0, 0.0), (0.3, 0.2), (1.0, 0.5), (2.0, 0.8), (3.0, 1.0)]
TYPE_PRIOR = {"실적": 0.8, "수주/공급계약": 0.7, "수주": 0.7, "M&A/지분": 0.6, "M&A": 0.6, "기술/제품": 0.5, "정책/규제": 0.5, "정책": 0.5, "공급망/원자재": 0.5, "거시/환율/금리": 0.3, "기타": 0.3}


def _cred(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    for f in CRED_FILES:
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ───────────────────────── 키움 클라이언트 ─────────────────────────
class Kiwoom:
    def __init__(self):
        self.mode = (_cred("KIWOOM_MODE") or "real").lower()
        self.base = "https://mockapi.kiwoom.com" if self.mode == "demo" else "https://api.kiwoom.com"
        self.appkey, self.secret = _cred("APP_KEY"), _cred("APP_SECRET")
        self._token: str | None = None
        self._exp = 0.0
        self._bars: dict[tuple[str, str], list[dict]] = {}

    @property
    def available(self) -> bool:
        return bool(self.appkey and self.secret)

    def token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        tf = CACHE / "kiwoom_token.json"
        if tf.exists():
            try:
                d = json.loads(tf.read_text())
                if d.get("mode") == self.mode and time.time() < d.get("exp", 0) - 60:
                    self._token, self._exp = d["token"], d["exp"]
                    return self._token
            except Exception:  # noqa: BLE001
                pass
        r = httpx.post(f"{self.base}/oauth2/token", json={"grant_type": "client_credentials", "appkey": self.appkey, "secretkey": self.secret},
                       headers={"Content-Type": "application/json;charset=UTF-8"}, timeout=15)
        r.raise_for_status()
        d = r.json()
        if d.get("return_code") not in (0, "0", None) or not d.get("token"):
            raise RuntimeError(f"키움 토큰 발급 실패: {d.get('return_msg')}")
        exp = datetime.strptime(d.get("expires_dt", ""), "%Y%m%d%H%M%S").replace(tzinfo=KST).timestamp() if d.get("expires_dt") else time.time() + 6 * 3600
        self._token, self._exp = d["token"], exp
        CACHE.mkdir(parents=True, exist_ok=True)
        tf.write_text(json.dumps({"token": self._token, "exp": exp, "mode": self.mode}))
        try:
            os.chmod(tf, 0o600)
        except OSError:
            pass
        return self._token

    def minute_bars(self, code: str, base_dt: str) -> list[dict]:
        """1분봉, 오래된→최신 순. 캐시: 당일 10분, 과거 영구(메모리+디스크)."""
        key = (code, base_dt)
        if key in self._bars:
            return self._bars[key]
        cf = CACHE / "minute" / f"{code}_{base_dt}.json"
        today = datetime.now(KST).strftime("%Y%m%d")
        if cf.exists() and (base_dt < today or time.time() - cf.stat().st_mtime < 600):
            rows = json.loads(cf.read_text())
            self._bars[key] = rows
            return rows
        r = httpx.post(f"{self.base}/api/dostk/chart", json={"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1", "base_dt": base_dt},
                       headers={"Content-Type": "application/json;charset=UTF-8", "authorization": f"Bearer {self.token()}", "api-id": "ka10080"}, timeout=20)
        r.raise_for_status()
        d = r.json()
        raw = d.get("stk_min_pole_chart_qry") or []
        rows = []
        for b in raw:
            try:
                rows.append({"t": b["cntr_tm"], "o": abs(int(b["open_pric"])), "h": abs(int(b["high_pric"])), "l": abs(int(b["low_pric"])), "c": abs(int(b["cur_prc"])), "v": int(b.get("trde_qty") or 0)})
            except (KeyError, ValueError):
                continue
        rows.sort(key=lambda x: x["t"])
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(rows))
        self._bars[key] = rows
        time.sleep(0.25)  # 유량 제한 배려
        return rows


# ───────────────────────── 반응 계산 ─────────────────────────
def _interp(x: float, curve: list[tuple[float, float]]) -> float:
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


def _bar_at_or_before(bars: list[dict], ts: str) -> dict | None:
    sel = [b for b in bars if b["t"] <= ts]
    return sel[-1] if sel else None


def _regular(bars: list[dict], day: str) -> list[dict]:
    return [b for b in bars if b["t"][:8] == day and f"{day}090000" <= b["t"] <= f"{day}153000"]


def _typical_3m_move(bars: list[dict]) -> float | None:
    moves = [abs(b["c"] / a["c"] - 1) * 100 for a, b in zip(bars, bars[3:]) if a["c"]]
    return statistics.median(moves) if len(moves) >= 20 else None


def reaction(kw: Kiwoom, code: str, published_at: str) -> dict:
    out: dict = {"status": "unavailable", "abs_move_pct": None, "score": None}
    try:
        t0 = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        t0 = t0.astimezone(KST) if t0.tzinfo else t0.replace(tzinfo=KST)
    except Exception:  # noqa: BLE001
        out["status"] = "발행시각 없음"
        return out
    if not kw.available:
        out["status"] = "키움 자격증명 없음"
        return out
    day = t0.strftime("%Y%m%d")
    in_session = t0.weekday() < 5 and "09:00" <= t0.strftime("%H:%M") < "15:30"
    try:
        bars = kw.minute_bars(code, day)
    except Exception as exc:  # noqa: BLE001
        out["status"] = f"분봉 조회 실패: {type(exc).__name__}"
        return out
    if in_session:
        reg = _regular(bars, day)
        pre = _bar_at_or_before(reg, (t0 - timedelta(minutes=1)).strftime("%Y%m%d%H%M00"))
        posts = [_bar_at_or_before(reg, (t0 + timedelta(minutes=k)).strftime("%Y%m%d%H%M00")) for k in (1, 2, 3)]
        posts = [p for p in posts if p and pre and p["t"] > pre["t"]]
        if pre and not posts and t0.strftime("%H:%M") >= "15:20":
            # 15:20~15:30 마감 동시호가: 체결 없음 → 15:30 종가(단일가)로 반응 측정
            close_bar = next((b for b in reg if b["t"] == f"{day}153000"), None)
            if close_bar:
                posts = [close_bar]
        if not pre or not posts:
            out["status"] = "분봉 부족(해당 시각 체결 없음)"
            return out
        win = [b for b in reg if pre["t"] <= b["t"] <= posts[-1]["t"]]
        moves = [abs(p["c"] / pre["c"] - 1) * 100 for p in posts]
        rng = (max(b["h"] for b in win) - min(b["l"] for b in win)) / pre["c"] * 100
        abs_move = max(moves + [rng])
        signed = (posts[-1]["c"] / pre["c"] - 1) * 100
        out.update({"status": "ok", "mode": "장중" if t0.strftime("%H:%M") < "15:20" else "마감 동시호가", "window": f"{pre['t'][8:12]}→{posts[-1]['t'][8:12]}", "pre_close": pre["c"], "post_close": posts[-1]["c"],
                    "signed_move_pct": round(signed, 2), "abs_move_pct": round(abs_move, 2), "range_pct": round(rng, 2),
                    "typical_3m_pct": (round(_typical_3m_move(reg), 2) if _typical_3m_move(reg) is not None else None)})
    else:
        # 장외: 직전 세션 종가 → 다음 세션 09:01~09:03
        prev_days = sorted({b["t"][:8] for b in bars if b["t"][:8] <= day and b["t"][8:14] <= "153000"})
        if t0.strftime("%H:%M") < "09:00" and t0.weekday() < 5:
            prev_days = [d for d in prev_days if d < day]
        if not prev_days:
            out["status"] = "직전 세션 분봉 없음"
            return out
        pday = prev_days[-1]
        pre = _bar_at_or_before(_regular(bars, pday), f"{pday}153000")
        next_day = None
        probe = t0.date() if (t0.strftime("%H:%M") < "09:00" and t0.weekday() < 5) else t0.date() + timedelta(days=1)
        for _ in range(7):
            if probe.weekday() < 5:
                nd = probe.strftime("%Y%m%d")
                try:
                    nb = kw.minute_bars(code, nd) if nd != day else bars
                except Exception:  # noqa: BLE001
                    nb = []
                if _regular(nb, nd):
                    next_day, nbars = nd, nb
                    break
            probe += timedelta(days=1)
        if not pre or not next_day:
            out["status"] = "개장 대기(다음 세션 분봉 없음)"
            out["pre_close"] = pre["c"] if pre else None
            return out
        reg = _regular(nbars, next_day)
        posts = [_bar_at_or_before(reg, f"{next_day}09{k:02d}00") for k in (1, 2, 3)]
        posts = [p for p in posts if p]
        if not posts:
            out["status"] = "개장 대기(다음 세션 분봉 없음)"
            return out
        win = [b for b in reg if b["t"] <= posts[-1]["t"]]
        moves = [abs(p["c"] / pre["c"] - 1) * 100 for p in posts]
        rng = (max(b["h"] for b in win) - min(b["l"] for b in win)) / pre["c"] * 100
        abs_move = max(moves + [rng])
        signed = (posts[-1]["c"] / pre["c"] - 1) * 100
        out.update({"status": "ok", "mode": "다음 개장", "window": f"{pday[4:]}종가→{next_day[4:]} 09:03", "pre_close": pre["c"], "post_close": posts[-1]["c"],
                    "signed_move_pct": round(signed, 2), "abs_move_pct": round(abs_move, 2), "range_pct": round(rng, 2), "typical_3m_pct": None})
    out["score"] = round(_interp(out["abs_move_pct"], REACT_CURVE), 3)
    return out


# ───────────────────────── 합산 ─────────────────────────
def fundamental(a: dict) -> dict:
    imp = a.get("impact") or {}
    if isinstance(imp.get("core_business"), (int, float)) and isinstance(imp.get("pnl_effect"), (int, float)):
        cb, pe = float(imp["core_business"]), float(imp["pnl_effect"])
        return {"core_business": cb, "pnl_effect": pe, "score": round(0.5 * cb + 0.5 * pe, 3), "source": "llm", "rationale": imp.get("rationale")}
    prior = TYPE_PRIOR.get((a.get("news_type") or "").strip(), 0.4)
    conf = float(a.get("confidence") or 0.5)
    est = round(prior * (0.6 + 0.4 * conf), 3)
    return {"core_business": None, "pnl_effect": None, "score": est, "source": "estimated", "rationale": f"뉴스 유형({a.get('news_type') or '기타'})·확신도({conf})로 추정 — 재분석하면 LLM 값으로 대체"}


def compute(a: dict, kw: Kiwoom | None = None) -> dict:
    kw = kw or Kiwoom()
    fund = fundamental(a)
    code = (a.get("post") or {}).get("primary_stock_id") or next((b.get("stock_id") for b in a.get("beneficiaries", [])), None)
    pub = (a.get("news") or {}).get("published_at") or ""
    react = reaction(kw, code, pub) if code else {"status": "대표 종목 없음", "abs_move_pct": None, "score": None}
    react["stock_id"] = code
    if react.get("score") is not None:
        score = round(100 * (WEIGHT_FUND * fund["score"] + WEIGHT_REACT * react["score"]))
        basis = "fund+reaction"
    else:
        score = round(100 * fund["score"])
        basis = "fund_only"
    return {**{k: v for k, v in (a.get("impact") or {}).items() if k in ("core_business", "pnl_effect", "rationale")},
            "fundamental": fund, "reaction": react, "score": score, "basis": basis, "computed_at": datetime.now(KST).isoformat(timespec="seconds")}


def apply(path: Path, kw: Kiwoom | None = None, force: bool = False) -> dict | None:
    a = json.loads(path.read_text(encoding="utf-8"))
    if a.get("skip"):
        return None
    cur = a.get("impact") or {}
    if cur.get("score") is not None and not force and (cur.get("reaction") or {}).get("status") == "ok":
        return cur
    a["impact"] = compute(a, kw)
    path.write_text(json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
    return a["impact"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update"); u.add_argument("--force", action="store_true")
    o = sub.add_parser("one"); o.add_argument("path"); o.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    kw = Kiwoom()
    paths = [Path(a.path)] if a.cmd == "one" else sorted(p for p in ANALYSIS_DIR.glob("*/*.json") if p.parent.name != "sample")
    for p in paths:
        try:
            imp = apply(p, kw, a.force)
        except Exception as exc:  # noqa: BLE001
            print(f"  실패 {p.name}: {type(exc).__name__}: {exc}")
            continue
        if imp:
            r = imp["reaction"]
            print(f"  {p.name} score={imp['score']:>3} fund={imp['fundamental']['score']:.2f}({imp['fundamental']['source'][:3]}) react={r.get('score')} {r.get('status')} {r.get('mode','')} {r.get('window','')} move={r.get('signed_move_pct')}% abs={r.get('abs_move_pct')}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
