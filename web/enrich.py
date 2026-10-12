"""상세 페이지용 보강 데이터 — 기사 대표 이미지(og:image), 종목 시세·1개월 스파크라인, 테마/산업 지표, 영향 경로·본문 블록 파싱.
외부 호출은 전부 캐시(data/cache/*.json)하고 실패해도 페이지는 뜬다."""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
API = "https://api.ant.wiki/api/v1"
UA = "Mozilla/5.0 (compatible; antwiki-news-curator/0.1)"
KST = timezone(timedelta(hours=9))
_lock = threading.Lock()


def _load(name: str) -> dict:
    p = CACHE / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save(name: str, d: dict) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    with _lock:
        (CACHE / name).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


# ───────── og:image ─────────
_og = _load("og.json")


def og_image(url: str | None, fetch: bool = True) -> str | None:
    """기사 대표 이미지. t.me 링크·실패는 None. 캐시 영구."""
    if not url or "t.me/" in url:
        return None
    if url in _og:
        return _og[url] or None
    if not fetch:
        return None
    img = None
    try:
        r = httpx.get(url, headers={"User-Agent": UA}, timeout=10, follow_redirects=True)
        m = re.search(r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', r.text, re.I) or \
            re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image', r.text, re.I)
        if m:
            img = m.group(1).strip()
            if img.startswith("//"):
                img = "https:" + img
    except Exception:  # noqa: BLE001
        img = None
    _og[url] = img or ""
    _save("og.json", _og)
    return img


def prefetch_og(urls: list[str]) -> None:
    """홈 진입 시 백그라운드로 썸네일 채우기 (미캐시 건만, 순차·조용히)."""
    todo = [u for u in urls if u and "t.me/" not in u and u not in _og]
    if not todo:
        return
    def _run():
        for u in todo[:60]:
            og_image(u)
            time.sleep(0.2)
    threading.Thread(target=_run, daemon=True).start()


# ───────── 시세 / 스파크라인 ─────────
_quotes: dict = {"at": 0, "data": {}}
_spark = _load("spark.json")


def quotes(codes: list[str]) -> dict[str, dict]:
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return {}
    now = time.time()
    missing = [c for c in codes if c not in _quotes["data"]] if now - _quotes["at"] < 60 else codes
    if missing:
        try:
            r = httpx.get(f"{API}/market/stocks", params={"codes": ",".join(missing)}, timeout=10, headers={"User-Agent": UA})
            found = r.json().get("found", {})
            _quotes["data"].update(found)
            _quotes["at"] = now
        except Exception:  # noqa: BLE001
            pass
    return {c: _quotes["data"].get(c) for c in codes if _quotes["data"].get(c)}


def closes_1m(code: str) -> list[dict]:
    ent = _spark.get(code)
    if ent and time.time() - ent.get("at", 0) < 1800:
        return ent["rows"]
    try:
        r = httpx.get(f"{API}/stock/{code}/prices", params={"period": "1M"}, timeout=10, headers={"User-Agent": UA})
        rows = [{"d": p["date"], "c": p["close"]} for p in sorted(r.json().get("prices", []), key=lambda p: p["date"])]
    except Exception:  # noqa: BLE001
        rows = []
    _spark[code] = {"at": time.time(), "rows": rows}
    _save("spark.json", _spark)
    return rows


def sparkline_svg(code: str, width: int = 160, height: int = 44) -> dict:
    """1개월 종가 스파크라인 SVG (단일 시리즈, 2px 선, 끝점 마커, 데이터 ends 라운드). 색은 1개월 등락 방향(한국식: 상승 빨강·하락 파랑)."""
    rows = closes_1m(code)
    if len(rows) < 2:
        return {"svg": "", "chg": None, "first": None, "last": None}
    vals = [r["c"] for r in rows]
    lo, hi = min(vals), max(vals)
    pad = 4
    span = (hi - lo) or 1
    pts = []
    for i, v in enumerate(vals):
        x = pad + (width - 2 * pad) * i / (len(vals) - 1)
        y = pad + (height - 2 * pad) * (1 - (v - lo) / span)
        pts.append((round(x, 1), round(y, 1)))
    chg = (vals[-1] / vals[0] - 1) * 100
    color = "var(--accent-red)" if chg >= 0 else "var(--accent-blue)"
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    area = d + f" L{pts[-1][0]},{height - pad} L{pts[0][0]},{height - pad} Z"
    lx, ly = pts[-1]
    svg = (
        f'<svg class="aw-spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{code} 1개월 종가 추이 {chg:+.1f}%">'
        f'<title>{rows[0]["d"]} {vals[0]:,.0f} → {rows[-1]["d"]} {vals[-1]:,.0f} ({chg:+.1f}%)</title>'
        f'<path d="{area}" fill="{color}" fill-opacity="0.08"/>'
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{lx}" cy="{ly}" r="3.5" fill="{color}" stroke="#fff" stroke-width="2"/></svg>'
    )
    return {"svg": svg, "chg": round(chg, 2), "first": rows[0], "last": rows[-1]}


def _spark_svg(vals: list[float], chg: float, label: str, title: str, width: int = 160, height: int = 44, baseline: float | None = None,
               marker: dict | None = None) -> str:
    """단일 시리즈 스파크라인. baseline(전일 종가)이 있으면 점선 기준선, marker={"i": 인덱스, "label": "뉴스 10:53", "edge": None|"left"|"right"} 가 있으면 뉴스 시점을 표시."""
    if len(vals) < 2:
        return ""
    lo, hi = min(vals + ([baseline] if baseline else [])), max(vals + ([baseline] if baseline else []))
    pad, span = 4, (hi - lo) or 1
    pts = [(round(pad + (width - 2 * pad) * i / (len(vals) - 1), 1), round(pad + (height - 2 * pad) * (1 - (v - lo) / span), 1)) for i, v in enumerate(vals)]
    color = "var(--accent-red)" if chg >= 0 else "var(--accent-blue)"
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    area = d + f" L{pts[-1][0]},{height - pad} L{pts[0][0]},{height - pad} Z"
    lx, ly = pts[-1]
    base = ""
    if baseline:
        by = round(pad + (height - 2 * pad) * (1 - (baseline - lo) / span), 1)
        base = f'<line x1="{pad}" y1="{by}" x2="{width - pad}" y2="{by}" stroke="var(--border-default)" stroke-width="1" stroke-dasharray="3 3"/>'
    mark = ""
    if marker and marker.get("i") is not None and 0 <= marker["i"] < len(pts):
        mx, my = pts[marker["i"]]
        mlabel = marker.get("label", "뉴스")
        mark = (f'<g class="aw-spark-news"><title>{mlabel}</title>'
                f'<line x1="{mx}" y1="{pad - 2}" x2="{mx}" y2="{height - pad}" stroke="var(--accent-orange)" stroke-width="1.5" stroke-dasharray="2 2"/>'
                f'<circle cx="{mx}" cy="{my}" r="4" fill="var(--accent-orange)" stroke="#fff" stroke-width="1.5"/>'
                f'<polygon points="{mx - 4},{pad - 2} {mx + 4},{pad - 2} {mx},{pad + 3}" fill="var(--accent-orange)"/></g>')
    elif marker and marker.get("edge"):
        ex = width - pad if marker["edge"] == "right" else pad
        mark = (f'<g class="aw-spark-news"><title>{marker.get("label", "뉴스")}</title>'
                f'<polygon points="{ex - 4},{pad - 2} {ex + 4},{pad - 2} {ex},{pad + 3}" fill="var(--accent-orange)"/></g>')
    return (f'<svg class="aw-spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{label} {chg:+.1f}%"><title>{title}</title>'
            f'{base}<path d="{area}" fill="{color}" fill-opacity="0.08"/><path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{lx}" cy="{ly}" r="3.5" fill="{color}" stroke="#fff" stroke-width="2"/>{mark}</svg>')


def sparkline_1d(code: str, prev_close: float | None = None, published_at: str | None = None) -> dict:
    """당일(또는 최근 거래일) 정규장 1분봉 스파크라인. 수익률은 전일 종가(quotes.prev_close) 대비, 없으면 첫 봉 시가 대비.
    published_at(뉴스 발행 시각)이 차트 날짜 안이면 그 시점을 주황 마커로 표시한다 (장 시작 전/마감 후는 좌/우 가장자리 삼각 표시)."""
    try:
        from pipeline.impact import Kiwoom
        kw = Kiwoom()
        if not kw.available:
            return {"svg": "", "chg": None}
        today = datetime.now(KST).strftime("%Y%m%d")
        bars = kw.minute_bars(code, today)
    except Exception:  # noqa: BLE001
        return {"svg": "", "chg": None}
    days = sorted({b["t"][:8] for b in bars})
    if not days:
        return {"svg": "", "chg": None}
    day = days[-1]
    reg = [b for b in bars if b["t"][:8] == day and f"{day}090000" <= b["t"] <= f"{day}153000"]
    if len(reg) < 2:
        return {"svg": "", "chg": None}
    vals = [b["c"] for b in reg]
    base = prev_close or reg[0]["o"]
    chg = (vals[-1] / base - 1) * 100 if base else 0.0
    title = f"{day[4:6]}.{day[6:]} {reg[0]['t'][8:10]}:{reg[0]['t'][10:12]}~{reg[-1]['t'][8:10]}:{reg[-1]['t'][10:12]} · 전일 종가 {base:,.0f} → {vals[-1]:,.0f} ({chg:+.2f}%)"
    marker = None
    news_time = None
    if published_at:
        try:
            t0 = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            t0 = t0.astimezone(KST) if t0.tzinfo else t0.replace(tzinfo=KST)
            if t0.strftime("%Y%m%d") == day:
                key = t0.strftime("%Y%m%d%H%M00")
                news_time = t0.strftime("%H:%M")
                if key < reg[0]["t"]:
                    marker = {"edge": "left", "label": f"뉴스 {news_time} (장 시작 전)"}
                elif key > reg[-1]["t"]:
                    marker = {"edge": "right", "label": f"뉴스 {news_time} (장 마감 후)"}
                else:
                    idx = next((i for i, b in enumerate(reg) if b["t"] >= key), len(reg) - 1)
                    marker = {"i": idx, "label": f"뉴스 {news_time} · {reg[idx]['c']:,.0f}원"}
        except Exception:  # noqa: BLE001
            marker = None
    return {"svg": _spark_svg(vals, chg, f"{code} 당일 분봉", title, baseline=base, marker=marker), "chg": round(chg, 2), "day": day,
            "news_time": news_time, "news_edge": (marker or {}).get("edge")}


# ───────── 테마/산업 지표 ─────────
def theme_stats(kb, slug: str) -> dict | None:
    t = kb.theme(slug)
    if t:
        cat = _category(slug_cat=t.get("category_slug"))
        m = t.get("metrics") or {}
        return {"kind": "theme", "slug": slug, "name": t["name"], "category": t.get("category_name"), "color": (cat or {}).get("color", "#f37022"),
                "url": t["url"], "r1": m.get("return_1d"), "r5": m.get("return_5d"), "rs": m.get("rs"), "n": t.get("stock_count"),
                "leaders": [s["name"] for s in t["stocks"] if s.get("is_leader")][:3] or [s["name"] for s in t["stocks"][:3]],
                "as_of": (t.get("as_of") or {}).get("rs_trade_date")}
    cat = _category(slug)
    if cat:
        m = cat.get("metrics") or {}
        return {"kind": "category", "slug": slug, "name": cat["name"], "category": None, "color": cat.get("color", "#f37022"),
                "url": f"https://www.ant.wiki/themes/{slug}", "r1": m.get("return_1d"), "r5": m.get("return_5d"), "rs": m.get("rs_score"),
                "n": m.get("stock_count"), "leaders": [], "as_of": None}
    return None


_cats: dict[str, dict] = {}


def _category(slug: str | None = None, slug_cat: str | None = None) -> dict | None:
    key = slug_cat or slug
    if not key:
        return None
    if key not in _cats:
        p = ROOT / "data" / "kb" / "categories" / f"{key}.json"
        _cats[key] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        if _cats[key] is not None and "color" not in _cats[key]:
            # wiki-index 에 색이 있음
            idx = ROOT / "data" / "kb" / "theme_wiki_index.json"
            if idx.exists():
                for c in json.loads(idx.read_text(encoding="utf-8")).get("categories", []):
                    if c.get("slug") == key:
                        _cats[key]["color"] = c.get("color")
    return _cats.get(key)


# ───────── 텍스트 → 구조 ─────────
def impact_steps(text: str | None) -> list[str]:
    if not text:
        return []
    parts = [p.strip(" .") for p in re.split(r"\s*(?:→|->|⇒)\s*", text) if p.strip()]
    return parts[:6]


def body_blocks(body: str | None) -> list[dict]:
    """게시 초안 본문을 블록으로: title / h3 / p / ul / link / note / disclaimer."""
    if not body:
        return []
    blocks: list[dict] = []
    lines = body.split("\n")
    first = True
    ul: list[str] = []
    def flush():
        nonlocal ul
        if ul:
            blocks.append({"t": "ul", "items": ul}); ul = []
    for raw in lines:
        ln = raw.strip()
        if not ln:
            flush(); continue
        if first:
            first = False
            blocks.append({"t": "title", "text": re.sub(r"^\[[^\]]*\]\s*", "", ln)}); continue
        if ln.startswith("■") or (ln.startswith("**") and ln.endswith("**")) or re.match(r"^#{1,3}\s", ln):
            flush(); blocks.append({"t": "h3", "text": ln.strip("■#* ").strip()}); continue
        if ln.startswith("- ") or ln.startswith("• "):
            ul.append(ln[2:].strip()); continue
        if ln.startswith("→"):
            flush()
            m = re.search(r"(https?://\S+)", ln)
            blocks.append({"t": "link", "label": ln.strip("→ ").split(":")[0].strip() if m else ln.strip("→ "), "url": m.group(1) if m else None}); continue
        if "투자 판단과 책임은 본인에게" in ln:
            flush(); blocks.append({"t": "disclaimer", "text": ln}); continue
        if ln.startswith("(") and ("수치 출처" in ln or "기준" in ln):
            flush(); blocks.append({"t": "note", "text": ln.strip("()")}); continue
        flush()
        # 본문 안의 (출처: …URL) → 링크화
        html_ln = re.sub(r"(https?://[^\s)]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', ln)
        html_ln = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html_ln)
        blocks.append({"t": "p", "html": html_ln})
    flush()
    return blocks
