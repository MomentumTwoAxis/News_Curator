"""Stage 2 — 수혜주 분석 (LLM 자동).

사용:
  python -m pipeline.analyze --url https://…                 # 링크 1건: 본문 추출 → KB 컨텍스트 팩 → LLM → data/analysis/{date}/{id}.json
  python -m pipeline.analyze --article-id <id>               # data/news/*.jsonl 의 수집 기사 1건 분석
  python -m pipeline.analyze --pending [--limit 5]           # 미분석 수집 기사 순차 분석 (속보형 자동화 루프용)
  python -m pipeline.analyze --url … --dry-run               # LLM 호출 없이 프롬프트(컨텍스트 팩)만 출력

LLM 공급자 (환경변수 LLM_PROVIDER=anthropic|gemini, 없으면 자격증명 있는 쪽 자동):
  anthropic — ANTHROPIC_API_KEY (또는 `ant auth login` 프로필). 모델 ANTHROPIC_MODEL (기본 claude-opus-5). 산업-테마 지도는 prompt caching.
  gemini    — GEMINI_API_KEY. 모델 GEMINI_MODEL (기본 gemini-2.5-flash). (텔레그램 봇과 같은 키 재사용 가능)

출력 JSON 계약: prompts/analyze_news.md (PLAN.md 4-3). 발행은 pipeline/publish.py 가 담당.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from kb.query import KB

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "analyze_news.md"
MAP_PATH = ROOT / "data" / "kb" / "brief" / "industry_theme_map.md"
NEWS_DIR = ROOT / "data" / "news"
ANALYSIS_DIR = ROOT / "data" / "analysis"
UA = "Mozilla/5.0 (compatible; antwiki-news-curator/0.1)"


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, "").strip()
    if v:
        return v
    # 폴백: 이 프로젝트 .env → 텔레그램 봇 프로젝트 .env (같은 키를 공유; 값은 로그에 쓰지 않는다)
    for envf in (ROOT / ".env", ROOT.parent / "antwiki_telegram_bot_v2" / ".env"):
        if envf.exists():
            for line in envf.read_text(encoding="utf-8").splitlines():
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


# ───────────────────────── 기사 본문 추출 ─────────────────────────
def fetch_article(url: str) -> dict:
    """제목·본문·매체·발행시각을 최대한 뽑는다 (og 태그 + 본문 후보 블록). 본문은 분석용으로만 쓰고 저장은 1,500자까지."""
    from bs4 import BeautifulSoup
    r = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"}, timeout=20, follow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    title = meta("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else url)
    source = meta("og:site_name", "twitter:site") or re.sub(r"^www\.", "", httpx.URL(str(r.url)).host or "")
    published = meta("article:published_time", "og:article:published_time", "pubdate", "date", "article:modified_time")
    desc = meta("og:description", "description")

    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe", "noscript"]):
        t.decompose()
    cands = soup.select("article, [itemprop=articleBody], #articleBody, #article-body, .article_body, .article-body, #newsct_article, .news_end, #articeBody, .article_view, #content, main")
    best = ""
    for c in cands:
        txt = c.get_text("\n", strip=True)
        if len(txt) > len(best):
            best = txt
    if len(best) < 300:
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        best = "\n".join(p for p in paras if len(p) > 30)
    body = re.sub(r"\n{3,}", "\n\n", best).strip()
    return {"title": re.sub(r"\s+", " ", title), "source": source, "published_at": published or datetime.now(KST).isoformat(),
            "url": str(r.url), "summary": (desc or body[:300]), "body": body[:8000]}


# ───────────────────────── 프롬프트 조립 ─────────────────────────
def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_messages(article: dict, kb: KB, segment: str | None) -> tuple[str, str, str]:
    """(system_stable, system_map, user) — 안정 지시문·산업지도는 캐싱 블록, 기사+컨텍스트 팩은 user."""
    system_stable = (
        "너는 앤트위키(www.ant.wiki)의 뉴스 큐레이터다. 아래 지시문의 절차와 출력 JSON 계약을 따른다. "
        "출력은 JSON 하나만, 다른 텍스트 없이. 수치는 제공된 컨텍스트 팩의 값만 쓰고 기준일을 붙인다.\n\n" + load_prompt()
    )
    system_map = MAP_PATH.read_text(encoding="utf-8") if MAP_PATH.exists() else "(산업-테마 지도 없음: python -m kb.build_kb 실행 필요)"
    text = f"{article.get('title','')}. {article.get('summary','')} {article.get('body','')[:3000]}"
    pack = kb.context_pack(text, segment=segment)
    user = (
        f"## 기사\n- id: {article.get('id')}\n- 제목: {article.get('title')}\n- 매체: {article.get('source')}\n- 발행: {article.get('published_at')}\n- URL: {article.get('url')}\n\n"
        f"### 요약\n{article.get('summary','')}\n\n### 본문\n{(article.get('body') or '')[:6000]}\n\n"
        f"## 앤트위키 컨텍스트 팩\n{pack}\n\n"
        "## 지시\n1) 먼저 specific_segment를 좁게 정하고, 2) 컨텍스트 팩의 '이 회사를 위키 본문에서 언급한 종목'을 tier 1/2 후보로 우선 검토하고, "
        "3) 세그먼트 밖 사업부 종목은 제외하고, 4) 출력 JSON 계약대로 답하라. id 필드는 위 id를 그대로 쓴다."
    )
    return system_stable, system_map, user


# ───────────────────────── LLM 호출 ─────────────────────────
def _provider() -> str:
    p = _env("LLM_PROVIDER").lower()
    if p in ("anthropic", "gemini"):
        return p
    if _env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN") or (Path.home() / ".config" / "anthropic").exists():
        return "anthropic"
    if _env("GEMINI_API_KEY"):
        return "gemini"
    return "none"


def _extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def call_anthropic(system_stable: str, system_map: str, user: str) -> tuple[dict, dict]:
    import anthropic
    client = anthropic.Anthropic()
    model = _env("ANTHROPIC_MODEL", "claude-opus-5")
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": _env("ANTHROPIC_EFFORT", "medium")},
        system=[
            {"type": "text", "text": system_stable, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            {"type": "text", "text": system_map, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        ],
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"모델이 응답을 거절함: {getattr(msg, 'stop_details', None)}")
    text = "".join(b.text for b in msg.content if b.type == "text")
    usage = {"provider": "anthropic", "model": model, "input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens,
             "cache_read": getattr(msg.usage, "cache_read_input_tokens", 0), "cache_write": getattr(msg.usage, "cache_creation_input_tokens", 0)}
    return _extract_json(text), usage


def call_gemini(system_stable: str, system_map: str, user: str) -> tuple[dict, dict]:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=_env("GEMINI_API_KEY"))
    model = _env("GEMINI_MODEL", "gemini-2.5-flash")
    resp = client.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(system_instruction=system_stable + "\n\n# 앤트위키 산업-테마 지도\n" + system_map,
                                           response_mime_type="application/json", temperature=0.2),
    )
    um = getattr(resp, "usage_metadata", None)
    usage = {"provider": "gemini", "model": model, "input_tokens": getattr(um, "prompt_token_count", None), "output_tokens": getattr(um, "candidates_token_count", None)}
    try:
        return _extract_json(resp.text or ""), usage
    except json.JSONDecodeError:
        # 긴 본문에서 JSON이 깨지는 경우가 있어 1회 재시도 (temperature 0, 본문 길이 제한 요청)
        resp = client.models.generate_content(
            model=model, contents=user + "\n\n(중요) 반드시 유효한 JSON 하나만 출력하고, body_text는 2,000자 이내로 작성하라.",
            config=types.GenerateContentConfig(system_instruction=system_stable + "\n\n# 앤트위키 산업-테마 지도\n" + system_map,
                                               response_mime_type="application/json", temperature=0.0),
        )
        usage["retried"] = True
        return _extract_json(resp.text or ""), usage


def analyze(article: dict, kb: KB | None = None, segment: str | None = None, dry_run: bool = False) -> dict:
    kb = kb or KB()
    if not article.get("id"):
        article["id"] = hashlib.sha1((article.get("url") or article.get("title", "")).encode()).hexdigest()[:16]
    system_stable, system_map, user = build_messages(article, kb, segment)
    if dry_run:
        return {"id": article["id"], "dry_run": True, "prompt_user": user, "system_chars": len(system_stable) + len(system_map)}
    prov = _provider()
    if prov == "anthropic":
        result, usage = call_anthropic(system_stable, system_map, user)
    elif prov == "gemini":
        result, usage = call_gemini(system_stable, system_map, user)
    else:
        raise RuntimeError("LLM 자격증명이 없습니다. ANTHROPIC_API_KEY 또는 GEMINI_API_KEY 를 설정하세요 (.env 가능).")
    result.setdefault("id", article["id"])
    result.setdefault("news", {})
    for k in ("title", "url", "source", "published_at"):
        result["news"].setdefault(k, article.get(k))
    result["news"].setdefault("summary", (article.get("summary") or "")[:500])
    result["meta"] = {"analyzed_at": datetime.now(KST).isoformat(timespec="seconds"), **usage, "segment_hint": segment}
    try:
        from pipeline import impact as _impact
        if not result.get("skip"):
            result["impact"] = _impact.compute(result)
    except Exception as exc:  # noqa: BLE001
        log(f"  주가 영향력 계산 실패({type(exc).__name__}) — 나중에 pipeline.impact update 로 보완")
    return result


def save(result: dict, day: str | None = None) -> Path:
    day = day or datetime.now(KST).strftime("%Y%m%d")
    d = ANALYSIS_DIR / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{result['id']}.json"
    p.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


# ───────────────────────── 수집 기사 접근 ─────────────────────────
def load_articles(days: int = 3) -> list[dict]:
    rows: list[dict] = []
    for p in sorted(NEWS_DIR.glob("*.jsonl"))[-days:]:
        rows += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows


def analyzed_ids() -> set[str]:
    return {p.stem for p in ANALYSIS_DIR.glob("*/*.json")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--article-id")
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--segment")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=1, help="--pending 병렬 수 (LLM 동시 호출)")
    a = ap.parse_args(argv)
    kb = KB()
    targets: list[dict] = []
    if a.url:
        art = fetch_article(a.url)
        art["id"] = hashlib.sha1(re.sub(r"[?#].*$", "", art["url"]).encode()).hexdigest()[:16]
        targets = [art]
    elif a.article_id:
        targets = [r for r in load_articles() if r["id"] == a.article_id]
    elif a.pending:
        done = analyzed_ids()
        targets = [r for r in load_articles() if r["id"] not in done][-a.limit:]
    else:
        ap.error("--url / --article-id / --pending 중 하나")
    log(f"분석 대상 {len(targets)}건 · provider={_provider()} · workers={a.workers}")

    def _one(art: dict) -> None:
        try:
            res = analyze(art, kb, a.segment, a.dry_run)
        except Exception as exc:  # noqa: BLE001
            log(f"  실패 {art.get('id')} {str(art.get('title'))[:30]}: {type(exc).__name__}: {str(exc)[:200]}")
            return
        if a.dry_run:
            print(res["prompt_user"][:4000]); print(f"... (system {res['system_chars']}자)")
            return
        p = save(res)
        bene = ", ".join(f"{b.get('name')}({b.get('score')})" for b in res.get("beneficiaries", []))
        log(f"  저장 {p.name} · {str(art.get('title'))[:28]} · skip={res.get('skip')} · 수혜 {bene or '-'} · in={res['meta'].get('input_tokens')}")

    if a.workers > 1 and not a.dry_run:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            list(ex.map(_one, targets))
    else:
        for art in targets:
            _one(art)
    return 0


if __name__ == "__main__":
    sys.exit(main())
