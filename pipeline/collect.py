"""Stage 1 — 뉴스 수급.

사용:
  python -m pipeline.collect                 # sources.yaml 기준 수집 → data/news/{YYYYMMDD}.jsonl (append, 중복 제외)
  python -m pipeline.collect --dry-run       # 저장 없이 채택 기사만 출력
  python -m pipeline.collect --hot-themes 5  # 앤트위키 핫 테마 상위 N개 이름을 Naver 검색어에 추가
  python -m pipeline.collect --no-naver

각 기사 레코드:
  {id, title, summary, url, source, source_type, published_at, collected_at,
   match: {stocks:[{stock_id,name}], themes:[slug], expanded_themes:[slug]}}
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
import yaml

from kb.query import KB

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
NEWS_DIR = ROOT / "data" / "news"
SEEN_PATH = ROOT / "data" / "news" / "seen_ids.json"
SOURCES = ROOT / "pipeline" / "sources.yaml"
UA = "antwiki-news-curator/0.1"


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def article_id(url: str, title: str) -> str:
    key = re.sub(r"[?#].*$", "", url.strip()) or title
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return html.unescape(s).strip()


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fn in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            d = fn(s)
            return d if d.tzinfo else d.replace(tzinfo=KST)
        except Exception:  # noqa: BLE001
            continue
    return None


def fetch_rss(client: httpx.Client, src: dict, source_type: str) -> list[dict]:
    try:
        import feedparser  # 선택 의존성
    except ImportError:
        feedparser = None
    try:
        r = client.get(src["url"])
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log(f"  RSS 실패 {src['name']}: {exc}")
        return []
    items: list[dict] = []
    if feedparser:
        feed = feedparser.parse(r.content)
        for e in feed.entries:
            items.append({
                "title": clean(e.get("title", "")), "summary": clean(e.get("summary", "") or e.get("description", ""))[:600],
                "url": e.get("link", ""), "published_at": (parse_dt(e.get("published") or e.get("updated")) or datetime.now(KST)).isoformat(),
            })
    else:  # feedparser 없을 때 최소 파서
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            return []
        for it in root.iter("item"):
            g = lambda tag: (it.findtext(tag) or "")  # noqa: E731
            items.append({"title": clean(g("title")), "summary": clean(g("description"))[:600], "url": g("link").strip(),
                          "published_at": (parse_dt(g("pubDate")) or datetime.now(KST)).isoformat()})
    for it in items:
        it.update({"source": src["name"], "source_type": source_type})
    return items


def fetch_naver(client: httpx.Client, queries: list[str], display: int) -> list[dict]:
    cid, csec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not csec:
        log("  Naver API 키 없음(NAVER_CLIENT_ID/SECRET) → 건너뜀")
        return []
    out: list[dict] = []
    for q in queries:
        try:
            r = client.get("https://openapi.naver.com/v1/search/news.json", params={"query": q, "display": display, "sort": "date"},
                           headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec})
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log(f"  Naver 실패 '{q}': {exc}")
            continue
        for it in r.json().get("items", []):
            out.append({"title": clean(it.get("title")), "summary": clean(it.get("description"))[:600], "url": it.get("originallink") or it.get("link"),
                        "published_at": (parse_dt(it.get("pubDate")) or datetime.now(KST)).isoformat(), "source": "naver_search", "source_type": "naver", "query": q})
        time.sleep(0.2)
    return out


# ── 같은 재료 중복 제거 (제목 토큰 Jaccard; 텔레그램 봇 STEP 3.5 이식) ──────────────
TITLES_PATH = ROOT / "data" / "news" / "seen_titles.json"
_TOK_STOP = {"속보", "단독", "종합", "오늘", "내일", "올해", "지난", "최대", "역대", "규모", "관련", "기자", "뉴스", "체결", "발표", "밝혔다", "위해", "통해", "대한", "것으로", "있다", "한다", "했다"}


def title_tokens(title: str) -> set[str]:
    t = re.sub(r"[\[\]()'\"“”‘’…·,.!?:;~\-/|]", " ", title or "")
    toks = {w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", t) if w not in _TOK_STOP and not w.isdigit()}
    return toks


def is_same_story(a: set[str], b: set[str], thresh: float = 0.35, min_common: int = 2) -> bool:
    if not a or not b:
        return False
    common = a & b
    return len(common) >= min_common and len(common) / len(a | b) >= thresh


def load_seen_titles(hours: int = 48) -> list[dict]:
    if not TITLES_PATH.exists():
        return []
    try:
        rows = json.loads(TITLES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    cutoff = (datetime.now(KST) - timedelta(hours=hours)).isoformat()
    return [r for r in rows if r.get("at", "") >= cutoff]


def save_seen_titles(rows: list[dict]) -> None:
    TITLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    TITLES_PATH.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def dedup_stories(items: list[dict], history: list[dict]) -> tuple[list[dict], int]:
    """배치 내 + 48h 이력과 제목 Jaccard로 같은 재료를 묶고, 각 클러스터에서 summary가 긴 1건만 남긴다."""
    hist_sets = [set(h["tokens"]) for h in history]
    kept: list[dict] = []
    kept_sets: list[set[str]] = []
    dropped = 0
    for it in sorted(items, key=lambda x: -len(x.get("summary") or "")):
        toks = title_tokens(it.get("title", ""))
        if any(is_same_story(toks, h) for h in hist_sets):
            dropped += 1
            continue
        dup_idx = next((i for i, k in enumerate(kept_sets) if is_same_story(toks, k)), None)
        if dup_idx is not None:
            kept[dup_idx].setdefault("also_reported_by", []).append(it.get("source"))
            dropped += 1
            continue
        it["_tokens"] = sorted(toks)
        kept.append(it)
        kept_sets.append(toks)
    return kept, dropped


def load_seen() -> dict[str, str]:
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_seen(seen: dict[str, str]) -> None:
    cutoff = (datetime.now(KST) - timedelta(days=7)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")


def hot_theme_names(n: int) -> list[str]:
    try:
        r = httpx.get("https://api.ant.wiki/api/v1/theme-page/hot-themes", params={"limit": n, "include_stocks": False}, timeout=20, headers={"User-Agent": UA})
        r.raise_for_status()
        data = r.json()
        items = data.get("themes") or data.get("items") or (data if isinstance(data, list) else [])
        return [t.get("display_name") or t.get("name") for t in items if (t.get("display_name") or t.get("name"))][:n]
    except Exception as exc:  # noqa: BLE001
        log(f"  핫 테마 조회 실패: {exc}")
        return []


def run(dry_run: bool, use_naver: bool, hot_n: int) -> list[dict]:
    cfg = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    flt = cfg["filters"]
    kb = KB()
    seen = load_seen()
    now = datetime.now(KST)
    cutoff = now - timedelta(hours=flt.get("lookback_hours", 6))
    raw: list[dict] = []
    with httpx.Client(timeout=20, headers={"User-Agent": UA}, follow_redirects=True) as client:
        for src in cfg["rss"].get("general", []):
            raw += fetch_rss(client, src, "general_rss")
        for src in cfg["rss"].get("specialized", []):
            raw += fetch_rss(client, src, "specialized_rss")
        if use_naver and cfg.get("naver", {}).get("enabled"):
            queries = list(cfg["naver"].get("queries", []))
            if hot_n:
                queries += hot_theme_names(hot_n)
            raw += fetch_naver(client, queries, int(cfg["naver"].get("display", 20)))
    log(f"수집 원문 {len(raw)}건")

    accepted: list[dict] = []
    stats = {"dup": 0, "old": 0, "excluded": 0, "excluded_rx": 0, "nomatch": 0}
    exclude_rx = [re.compile(p) for p in flt.get("exclude_title_patterns", [])]
    for it in raw:
        if not it.get("url") or not it.get("title"):
            continue
        aid = article_id(it["url"], it["title"])
        if aid in seen:
            stats["dup"] += 1
            continue
        pub = parse_dt(it.get("published_at"))
        if pub and pub < cutoff:
            stats["old"] += 1
            continue
        title = it["title"]
        if any(k in title for k in flt.get("exclude_title_keywords", [])) or any(p in it["url"] for p in flt.get("exclude_url_patterns", [])):
            stats["excluded"] += 1
            continue
        if any(rx.search(title) for rx in exclude_rx):
            stats["excluded_rx"] += 1
            continue
        m = kb.match(f"{title}. {it.get('summary', '')}", min_theme_alias_len=int(flt.get("min_theme_alias_len", 3)))
        strong_themes = [t for t in m["themes"] if t.get("alias_len", 0) >= int(flt.get("min_theme_alias_len", 3))]
        has_signal = any(k in title for k in flt.get("signal_keywords", []))
        ok = bool(m["stocks"]) or (bool(strong_themes) and (has_signal or not flt.get("theme_only_requires_signal", True)))
        if flt.get("require_match", True) and not ok:
            stats["nomatch"] += 1
            continue
        rec = {"id": aid, **it, "collected_at": now.isoformat(),
               "match": {"stocks": [{"stock_id": s["stock_id"], "name": s["name"], "has_wiki": s["has_wiki"]} for s in m["stocks"]],
                         "themes": [t["slug"] for t in m["themes"]], "expanded_themes": [t["slug"] for t in m["expanded_themes"]]}}
        accepted.append(rec)
        seen[aid] = now.isoformat()
    history = load_seen_titles()
    accepted, same_story = dedup_stories(accepted, history)
    log(f"채택 {len(accepted)}건 (중복 {stats['dup']}, 기간외 {stats['old']}, 제외어 {stats['excluded']}, 제외패턴 {stats['excluded_rx']}, 미매칭 {stats['nomatch']}, 같은재료 {same_story})")

    if dry_run:
        for a in accepted:
            extra = f"  (+{len(a['also_reported_by'])} 매체)" if a.get("also_reported_by") else ""
            print(f"- [{a['source']}] {a['title']}{extra}  → 종목 {[s['name'] for s in a['match']['stocks']]} 테마 {a['match']['themes']}")
        return accepted
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    out = NEWS_DIR / f"{now:%Y%m%d}.jsonl"
    with out.open("a", encoding="utf-8") as f:
        for a in accepted:
            toks = a.pop("_tokens", [])
            history.append({"at": now.isoformat(), "tokens": toks, "title": a["title"]})
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    save_seen(seen)
    save_seen_titles(history)
    log(f"저장 → {out.relative_to(ROOT)}")
    return accepted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-naver", action="store_true")
    ap.add_argument("--hot-themes", type=int, default=0)
    a = ap.parse_args(argv)
    run(a.dry_run, not a.no_naver, a.hot_themes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
