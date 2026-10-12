"""성과 추적 — 발행한 수혜주의 사후 수익률(D+1/D+5/D+20)을 앤트위키 시세 API로 채운다.

사용:
  python -m pipeline.track record data/analysis/20261012/abc.json     # 발행 직후 호출(publish --live 성공 시 자동 호출됨)
  python -m pipeline.track update                                      # 미완성 행의 D+1/5/20 수익률 채움 (매일 장 마감 후 1회)
  python -m pipeline.track report [--month 2026-10]                    # 적중률 요약 (월간 글 소재)

저장: data/track/records.jsonl — 1행 = 1(기사, 종목)
  {article_id, post_id, published_at, base_date, stock_id, name, role, score, kind(beneficiary|loser),
   base_close, d1, d5, d20 (수익률 %, 없으면 null), updated_at}
수익률 = (D+n 종가 / 발행일 종가 - 1) × 100. 발행일 종가는 발행이 15:30 이전이면 당일 종가, 이후면 다음 거래일 종가를 기준(base_date)으로 삼는다.
데이터 출처: GET /api/v1/stock/{ticker}/prices?period=3M (KRX 종가, 수정주가 반영 여부는 응답 adjustment_applied 참고)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone, time as dtime
from pathlib import Path

import httpx

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
TRACK = ROOT / "data" / "track" / "records.jsonl"
API = "https://api.ant.wiki/api/v1"
HORIZONS = {"d1": 1, "d5": 5, "d20": 20}


def _load() -> list[dict]:
    if not TRACK.exists():
        return []
    return [json.loads(l) for l in TRACK.read_text(encoding="utf-8").splitlines() if l.strip()]


def _save(rows: list[dict]) -> None:
    TRACK.parent.mkdir(parents=True, exist_ok=True)
    TRACK.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""), encoding="utf-8")


def _prices(code: str, period: str = "3M") -> list[dict]:
    r = httpx.get(f"{API}/stock/{code}/prices", params={"period": period}, timeout=20)
    r.raise_for_status()
    return sorted(r.json().get("prices", []), key=lambda p: p["date"])


def record(analysis_path: str, post_id: str | None = None) -> int:
    a = json.loads(Path(analysis_path).read_text(encoding="utf-8"))
    now = datetime.now(KST)
    base_date = now.date() if now.time() < dtime(15, 30) else now.date() + timedelta(days=1)  # 실제 거래일 보정은 update에서 종가 존재일로 처리
    rows = _load()
    existing = {(r["article_id"], r["stock_id"]) for r in rows}
    added = 0
    for kind in ("beneficiaries", "losers"):
        for b in a.get(kind, []):
            key = (a["id"], b["stock_id"])
            if key in existing:
                continue
            rows.append({"article_id": a["id"], "post_id": post_id, "news_title": (a.get("news") or {}).get("title"),
                         "published_at": now.isoformat(timespec="seconds"), "base_date": base_date.isoformat(),
                         "stock_id": b["stock_id"], "name": b["name"], "role": b.get("role"), "score": b.get("score"),
                         "kind": "beneficiary" if kind == "beneficiaries" else "loser",
                         "base_close": None, "d1": None, "d5": None, "d20": None, "updated_at": None})
            added += 1
    _save(rows)
    print(f"추적 등록 {added}건 → {TRACK.relative_to(ROOT)}")
    return 0


def update() -> int:
    rows = _load()
    todo = [r for r in rows if r["d20"] is None]
    cache: dict[str, list[dict]] = {}
    filled = 0
    for r in todo:
        code = r["stock_id"]
        try:
            px = cache.setdefault(code, _prices(code))
        except Exception as exc:  # noqa: BLE001
            print(f"  {code} 시세 실패: {exc}")
            continue
        dates = [p["date"] for p in px]
        # base: base_date 이상인 첫 거래일 종가
        idx = next((i for i, d in enumerate(dates) if d >= r["base_date"]), None)
        if idx is None:
            continue
        base = px[idx]["close"]
        r["base_close"] = base
        r["base_date"] = dates[idx]
        for k, n in HORIZONS.items():
            if r.get(k) is None and idx + n < len(px):
                r[k] = round((px[idx + n]["close"] / base - 1) * 100, 2)
        r["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
        filled += 1
    _save(rows)
    print(f"갱신 {filled}/{len(todo)}건")
    return 0


def report(month: str | None) -> int:
    rows = [r for r in _load() if r["kind"] == "beneficiary" and (not month or r["published_at"].startswith(month))]
    if not rows:
        print("데이터 없음")
        return 0
    print(f"수혜주 {len(rows)}건 (월: {month or '전체'})")
    for k in HORIZONS:
        vals = [r[k] for r in rows if r.get(k) is not None]
        if not vals:
            print(f"  {k.upper()}: 집계 전")
            continue
        hit = sum(1 for v in vals if v > 0)
        print(f"  {k.upper()}: n={len(vals)} 평균 {sum(vals)/len(vals):+.2f}% · 상승 비율 {hit/len(vals):.0%} · 중앙값 {sorted(vals)[len(vals)//2]:+.2f}%")
    # 역할별
    by_role: dict[str, list[float]] = {}
    for r in rows:
        if r.get("d5") is not None:
            by_role.setdefault(r.get("role") or "-", []).append(r["d5"])
    for role, vals in by_role.items():
        print(f"  role={role}: D5 평균 {sum(vals)/len(vals):+.2f}% (n={len(vals)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record"); r.add_argument("analysis"); r.add_argument("--post-id")
    sub.add_parser("update")
    rp = sub.add_parser("report"); rp.add_argument("--month")
    a = ap.parse_args(argv)
    if a.cmd == "record":
        return record(a.analysis, a.post_id)
    if a.cmd == "update":
        return update()
    return report(a.month)


if __name__ == "__main__":
    sys.exit(main())
