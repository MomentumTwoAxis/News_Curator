"""앤트위키 뉴스 큐레이터 — 로컬 웹 (FastAPI).

실행:  .venv/bin/uvicorn web.server:app --reload --port 8765   (또는 .claude/launch.json 의 news-curator)
화면:
  /                 뉴스 리스트(수집분, 분석 상태 표시) + 상단 '링크 분석' 입력
  /news/{id}        기사 1건: 우리 분석(수혜주·근거·게시 초안) + 원문으로 이동
  POST /api/analyze {url}  → 본문 추출 → KB 컨텍스트 팩 → LLM → 분석 저장 → {id}
  GET  /api/news    리스트 JSON · GET /api/analysis/{id} 분석 JSON
디자인: 앤트위키 서비스 셸(헤더 52px·Pretendard·820px 중앙 콘텍츠·좌우 레일)을 따라가고 가운데 본문만 우리 내용으로 채운다.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from kb.query import KB
from pipeline import analyze as an
from web import enrich

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
app = FastAPI(title="AntWiki News Curator (local)")
app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB / "templates"))
_kb: KB | None = None
_jobs: dict[str, dict] = {}  # 링크 분석 진행 상태 {id: {status, error}}


def kb() -> KB:
    global _kb
    if _kb is None:
        _kb = KB()
    return _kb


# ───────────────────────── 데이터 ─────────────────────────
def load_analyses() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in sorted(an.ANALYSIS_DIR.glob("*/*.json")):
        if p.parent.name == "sample":
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out[d.get("id") or p.stem] = d
        except Exception:  # noqa: BLE001
            continue
    return out


def load_news(days: int = 3) -> list[dict]:
    rows = an.load_articles(days)
    analyses = load_analyses()
    seen: set[str] = set()
    items: list[dict] = []
    # 수집 기사 + (수집에 없는) 링크 분석 결과도 리스트에 포함
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        a = analyses.get(r["id"])
        items.append(_card(r, a))
    for aid, a in analyses.items():
        if aid in seen:
            continue
        n = a.get("news") or {}
        items.append(_card({"id": aid, "title": n.get("title"), "summary": n.get("summary", ""), "url": n.get("url"), "source": n.get("source") or "link",
                            "source_type": "manual", "published_at": n.get("published_at") or (a.get("meta") or {}).get("analyzed_at", ""),
                            "match": {"stocks": [], "themes": []}}, a))
    items.sort(key=lambda x: x["published_at"] or "", reverse=True)
    return items


def _card(r: dict, a: dict | None) -> dict:
    bene = [b for b in (a or {}).get("beneficiaries", [])] if a else []
    return {
        "id": r["id"], "title": r.get("title") or "(제목 없음)", "summary": (r.get("summary") or "")[:160],
        "url": r.get("url"), "source": (r.get("source") or "").replace("tg:", "TG · "), "source_type": r.get("source_type"),
        "published_at": r.get("published_at") or "", "time": _fmt_time(r.get("published_at")),
        "stocks": [s["name"] for s in (r.get("match") or {}).get("stocks", [])][:6],
        "themes": [t for t in (r.get("match") or {}).get("themes", [])][:4],
        "analyzed": a is not None, "skip": bool((a or {}).get("skip")),
        "beneficiaries": [{"name": b.get("name"), "stock_id": b.get("stock_id"), "score": b.get("score"), "role": b.get("role")} for b in bene[:4]],
        "stance": ((a or {}).get("post") or {}).get("stance"), "confidence": (a or {}).get("confidence"),
        "news_type": (a or {}).get("news_type"),
        "impact_score": ((a or {}).get("impact") or {}).get("score"),
        "top_bene_score": max([b.get("score") or 0 for b in bene], default=None),
        # 하이라이트: 수혜 점수 0.9 이상인 종목이 있고 주가 영향력 90 이상
        "highlight": bool(bene) and max([b.get("score") or 0 for b in bene], default=0) >= 0.9 and (((a or {}).get("impact") or {}).get("score") or 0) >= 90,
    }


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        d = d.astimezone(KST) if d.tzinfo else d
        now = datetime.now(KST)
        if d.date() == now.date():
            return d.strftime("%H:%M")
        return d.strftime("%m.%d %H:%M")
    except Exception:  # noqa: BLE001
        return iso[:16]


def wiki_url(name: str, code: str) -> str:
    from kb.api import wiki_url as w
    return w(name, code)


def theme_url(slug: str) -> str:
    t = kb().theme(slug)
    return t["url"] if t else f"https://www.ant.wiki/themes/all"


templates.env.globals.update(wiki_url=wiki_url, theme_url=theme_url, fmt_time=_fmt_time)


# ───────────────────────── 페이지 ─────────────────────────
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    items = load_news()
    for it in items:
        it["image"] = enrich.og_image(it.get("url"), fetch=False)
    enrich.prefetch_og([it.get("url") for it in items if not it.get("image")])
    stats = {"total": len(items), "analyzed": sum(1 for i in items if i["analyzed"]), "highlight": sum(1 for i in items if i.get("highlight")), "provider": an._provider()}
    return templates.TemplateResponse(request, "home.html", {"items": items, "stats": stats, "jobs": _jobs, "active": "news"})


@app.get("/news/{aid}", response_class=HTMLResponse)
def detail(request: Request, aid: str):
    analyses = load_analyses()
    a = analyses.get(aid)
    art = next((r for r in an.load_articles(7) if r["id"] == aid), None)
    if not a and not art:
        raise HTTPException(404, "기사를 찾을 수 없습니다")
    news = (a or {}).get("news") or {}
    if art:
        for k in ("title", "url", "source", "published_at", "summary"):
            news.setdefault(k, art.get(k))
    pack = kb().context_pack(f"{news.get('title','')}. {news.get('summary','')}") if not a else None
    job = _jobs.get(aid)
    image = enrich.og_image(news.get("url"))
    ctx: dict = {"a": a, "news": news, "art": art, "pack": pack, "job": job, "aid": aid, "active": "news", "image": image}
    if a and not a.get("skip") and ((a.get("impact") or {}).get("score") is None or ((a.get("impact") or {}).get("reaction") or {}).get("status") != "ok"):
        try:
            from pipeline import impact as _impact
            p = next((p for p in an.ANALYSIS_DIR.glob(f"*/{aid}.json")), None)
            if p:
                a["impact"] = _impact.apply(p) or a.get("impact")
        except Exception:  # noqa: BLE001
            pass
    if a:
        codes = [b.get("stock_id") for b in a.get("beneficiaries", []) + a.get("losers", []) + a.get("watchlist", []) if b.get("stock_id")]
        ctx["quotes"] = enrich.quotes(codes)
        ctx["sparks"] = {c: enrich.sparkline_svg(c) for c in [b.get("stock_id") for b in a.get("beneficiaries", []) + a.get("losers", [])] if c}
        ctx["sparks_1d"] = {c: enrich.sparkline_1d(c, (ctx["quotes"].get(c) or {}).get("prev_close"), news.get("published_at"))
                            for c in [b.get("stock_id") for b in a.get("beneficiaries", []) if (b.get("score") or 0) >= 0.9] if c}
        ctx["themes"] = [t for t in (enrich.theme_stats(kb(), th.get("slug")) for th in a.get("themes", [])) if t]
        ctx["steps"] = enrich.impact_steps(a.get("impact_path"))
        ctx["blocks"] = enrich.body_blocks((a.get("post") or {}).get("body_text"))
        # 3단 수혜 논리 폴백: LLM logic 없으면 (영향 경로 / 위키 한줄설명 / reason)으로 구성
        first_sent = re.split(r"(?<=[.다요])\s", (a.get("news") or {}).get("summary") or "")[0][:120]
        for b in a.get("beneficiaries", []) + a.get("losers", []):
            lg = b.get("logic") or {}
            sm = kb().summary.get(b.get("stock_id") or "") or {}
            w = kb().wiki(b.get("stock_id") or "") or {}
            b["logic_view"] = {
                "news_meaning": lg.get("news_meaning") or a.get("impact_path") or first_sent,
                "stock_profile": lg.get("stock_profile") or w.get("one_liner") or sm.get("description") or "",
                "effect": lg.get("effect") or b.get("reason") or "",
                "source": "llm" if lg.get("effect") else "fallback",
            }
        ctx["big"] = [b for b in a.get("beneficiaries", []) if (b.get("score") or 0) >= 0.9]
        ctx["small"] = [b for b in a.get("beneficiaries", []) if (b.get("score") or 0) < 0.9]
        ctx["kb_count"] = sum(len(b.get("kb_evidence") or []) for b in a.get("beneficiaries", []))
        ctx["web_count"] = sum(len(b.get("web_evidence") or []) for b in a.get("beneficiaries", []))
    return templates.TemplateResponse(request, "detail.html", ctx)


# ───────────────────────── API ─────────────────────────
class AnalyzeReq(BaseModel):
    url: str
    segment: str | None = None


def _run_job(aid: str, art: dict, segment: str | None) -> None:
    try:
        res = an.analyze(art, kb(), segment)
        an.save(res)
        _jobs[aid] = {"status": "done"}
    except Exception as exc:  # noqa: BLE001
        _jobs[aid] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


@app.post("/api/analyze")
def api_analyze(req: AnalyzeReq):
    url = req.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "http(s) 링크를 입력하세요")
    try:
        art = an.fetch_article(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"본문을 가져오지 못했습니다: {type(exc).__name__}") from exc
    import hashlib
    aid = hashlib.sha1(re.sub(r"[?#].*$", "", art["url"]).encode()).hexdigest()[:16]
    art["id"] = aid
    if aid in load_analyses():
        return {"id": aid, "status": "done", "cached": True}
    if an._provider() == "none":
        raise HTTPException(503, "LLM 자격증명이 없습니다 (ANTHROPIC_API_KEY 또는 GEMINI_API_KEY)")
    # 수집 목록에도 남겨 리스트에서 보이게
    an.NEWS_DIR.mkdir(parents=True, exist_ok=True)
    with (an.NEWS_DIR / f"{datetime.now(KST):%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
        m = kb().match(f"{art['title']}. {art['summary']}")
        f.write(json.dumps({**{k: art[k] for k in ("id", "title", "summary", "url", "source", "published_at")}, "source_type": "manual",
                            "collected_at": datetime.now(KST).isoformat(),
                            "match": {"stocks": [{"stock_id": s["stock_id"], "name": s["name"], "has_wiki": s["has_wiki"]} for s in m["stocks"]],
                                      "themes": [t["slug"] for t in m["themes"]], "expanded_themes": []}}, ensure_ascii=False) + "\n")
    _jobs[aid] = {"status": "running"}
    threading.Thread(target=_run_job, args=(aid, art, req.segment), daemon=True).start()
    return {"id": aid, "status": "running"}


@app.post("/api/analyze/{aid}")
def api_analyze_existing(aid: str, segment: str | None = None):
    art = next((r for r in an.load_articles(7) if r["id"] == aid), None)
    if not art:
        raise HTTPException(404)
    if an._provider() == "none":
        raise HTTPException(503, "LLM 자격증명이 없습니다")
    _jobs[aid] = {"status": "running"}
    threading.Thread(target=_run_job, args=(aid, art, segment), daemon=True).start()
    return {"id": aid, "status": "running"}


@app.get("/api/jobs/{aid}")
def api_job(aid: str):
    if aid in load_analyses():
        return {"status": "done"}
    return _jobs.get(aid, {"status": "none"})


@app.get("/api/news")
def api_news():
    return load_news()


@app.get("/api/analysis/{aid}")
def api_analysis(aid: str):
    a = load_analyses().get(aid)
    if not a:
        raise HTTPException(404)
    return a


# ───────────────────────── 이미지 프록시 (디스크 캐시) ─────────────────────────
import hashlib as _hashlib
import httpx as _httpx
_IMG_DIR = ROOT / "data" / "cache" / "img"


@app.get("/img")
def img_proxy(u: str):
    """기사 og:image를 서버가 받아 캐시 후 서빙 — 외부 CDN 지연·핫링크 제한과 무관하게 썸네일이 즉시 뜬다."""
    if not re.match(r"^https?://", u):
        raise HTTPException(400)
    _IMG_DIR.mkdir(parents=True, exist_ok=True)
    key = _hashlib.sha1(u.encode()).hexdigest()
    meta = _IMG_DIR / f"{key}.json"
    blob = _IMG_DIR / f"{key}.bin"
    if blob.exists() and meta.exists():
        ct = json.loads(meta.read_text()).get("ct", "image/jpeg")
        return Response(blob.read_bytes(), media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    try:
        r = _httpx.get(u, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (compatible; antwiki-news-curator/0.1)", "Accept": "image/*"})
        r.raise_for_status()
        ct = r.headers.get("content-type", "image/jpeg").split(";")[0]
        if not ct.startswith("image/"):
            raise HTTPException(415)
        blob.write_bytes(r.content)
        meta.write_text(json.dumps({"ct": ct, "u": u}))
        return Response(r.content, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"image fetch failed: {type(exc).__name__}") from exc


def img_url(u: str | None) -> str | None:
    from urllib.parse import quote
    return f"/img?u={quote(u, safe='')}" if u else None


templates.env.globals["img_url"] = img_url


def asset_v() -> int:
    """정적 파일 캐시 무력화용 버전(최종 수정시각)."""
    try:
        return int(max(f.stat().st_mtime for f in (WEB / "static").iterdir() if f.is_file()))
    except Exception:  # noqa: BLE001
        return 0


templates.env.globals["asset_v"] = asset_v
