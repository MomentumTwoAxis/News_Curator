"""앤트위키 지식베이스 빌더.

사용:
  python -m kb.build_kb                 # 전체 빌드 (위키 514 + 테마 252 + 산업 19)
  python -m kb.build_kb --skip-wiki     # 테마/산업만
  python -m kb.build_kb --skip-themes   # 위키만
  python -m kb.build_kb --brief-only    # 받아둔 JSON으로 brief만 재생성
  python -m kb.build_kb --limit 20      # 테스트용 (위키·테마 각 20개)

산출물 (data/kb/):
  stock_names.json              전 상장종목 코드→이름
  wiki/{code}.json              종목 위키 레코드 (PLAN.md 4-1 계약)
  themes/{slug}.json            테마 레코드 (PLAN.md 4-2 계약)
  categories/{slug}.json        산업(카테고리) 레코드: 밸류체인 단계·트렌드·소속 테마
  index/name_to_code.json       종목명(정규화)→코드
  index/theme_stocks.json       테마 slug→[종목코드]
  index/stock_themes.json       종목코드→[{slug,name,category,is_leader,rank}]
  index/mention_index.json      기업코드→[그 기업을 본문에서 언급한 위키(종목, 섹션, 문장)]  ← 공급관계 근거용 역색인
  brief/industry_theme_map.md   분석 시 항상 적재하는 산업-테마 지도
  brief/stocks_index.md         분석 시 항상 적재하는 전 종목 한 줄 인덱스 (이름·코드·한줄설명·테마)
  brief/stocks_core.md          위키 보유 종목 상세 요약 (투자포인트·리스크·섹션) — 필요 시 적재
  stocks_summary.json           전 종목 목록(한줄설명·편집일) — /wiki/list 원본
  manifest.json                 빌드 시각·건수·테마 지표 기준일
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .api import AntWikiAPI, theme_url, wiki_url
from .pm_text import doc_to_sections

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "data" / "kb"


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def normalize_name(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ──────────────────────────── 위키 ────────────────────────────
def _narr_text(n: dict) -> str:
    from .pm_text import doc_to_text
    c = n.get("content")
    return doc_to_text(c) if isinstance(c, (dict, list)) else str(c or "")


def build_wiki_record(api: AntWikiAPI, item: dict) -> dict:
    code = item["stock_id"]
    full = api.wiki_full(code, narrative_limit=6)
    infobox = full.get("infobox") or {}
    sysf = infobox.get("system_fields") or {}
    tags_user = [t.get("label") for t in (infobox.get("tags") or {}).get("user", []) if t.get("label")]
    tags_sys = {t.get("type"): t.get("label") for t in (infobox.get("tags") or {}).get("system", [])}
    content = full.get("wiki_content") or {}
    doc = content.get("content")
    if isinstance(doc, list):
        doc = {"type": "doc", "content": doc}
    sections = doc_to_sections(doc)

    narratives = full.get("narratives") or {}
    if isinstance(narratives, list):  # 방어: 리스트 형태일 때 category로 분기
        grouped: dict[str, list] = {"bullish": [], "bearish": []}
        for n in narratives:
            grouped.setdefault(n.get("category", "bullish"), []).append(n)
        narratives = grouped

    def pick(cat: str) -> list[dict]:
        out = []
        bucket = narratives.get(cat) or []
        if isinstance(bucket, dict):  # {"items": [...], "total": n}
            bucket = bucket.get("items") or []
        for n in bucket[:4]:
            out.append({"text": _narr_text(n)[:600], "score": n.get("score"), "created_at": n.get("created_at")})
        return out

    themes_resp = api.stock_themes(code, limit=30)
    themes = [
        {
            "slug": t.get("slug"), "name": t.get("name"),
            "category_slug": t.get("categorySlug"), "category_name": t.get("categoryName"),
            "is_leader": bool(t.get("isLeader")), "rank": t.get("rank"),
            "stock_rs": t.get("stockRs"), "theme_rs": t.get("rs"),
        }
        for t in themes_resp.get("themes", [])
    ]
    one = api.one_liner(code) or {}

    rec = {
        "stock_id": code,
        "name": full.get("name") or item.get("name"),
        "description": full.get("description") or item.get("description"),
        "one_liner": one.get("text"),
        "exchange": sysf.get("exchange") or tags_sys.get("exchange"),
        "industry": sysf.get("industry") or tags_sys.get("industry"),
        "ceo": sysf.get("ceo"),
        "english_name": sysf.get("english_name"),
        "tags": tags_user,
        "investment_points": sysf.get("investment_points") or [],
        "risk_factors": sysf.get("risk_factors") or [],
        "user_fields": [{"label": f.get("label"), "value": f.get("value")} for f in infobox.get("user_fields", [])],
        "valuation": {k: sysf.get(k) for k in ("per", "pbr", "eps", "bps", "dividend_yield", "foreign_ratio", "market_cap")},
        "valuation_as_of": sysf.get("valuation_as_of"),
        "research_as_of": sysf.get("research_as_of"),
        "sections": sections,
        "narratives": {"bullish": pick("bullish"), "bearish": pick("bearish")},
        "themes": themes,
        "wiki_revision": content.get("revision_number"),
        "wiki_updated_at": content.get("updated_at"),
        "view_count": item.get("view_count"),
        "list_updated_at": item.get("updated_at"),
        "url": wiki_url(full.get("name") or item.get("name"), code),
    }
    return rec


def probe_sections(api: AntWikiAPI, item: dict) -> tuple[str, int, int | None]:
    try:
        d = api.wiki_sections(item["stock_id"])
        return item["stock_id"], len(d.get("sections") or []), d.get("revision_number")
    except Exception:  # noqa: BLE001  (404 등 → 본문 없음)
        return item["stock_id"], 0, None


def build_wiki(api: AntWikiAPI, limit: int | None, workers: int) -> list[dict]:
    items = api.wiki_list_all()  # name 오름차순, 전 종목(≈2,670)
    log(f"위키 목록 {len(items)}건 (한줄설명 보유 {sum(1 for i in items if i.get('description'))}건)")
    # 전 종목 요약(한줄설명)은 그대로 저장 → 이름 인덱스·compact brief에 사용
    dump(KB_DIR / "stocks_summary.json", {
        i["stock_id"]: {"name": i.get("name"), "description": i.get("description"),
                        "editorial_modified_at": i.get("editorial_modified_at"), "updated_at": i.get("updated_at")}
        for i in items
    })
    if limit:
        items = items[:limit]

    # 경량 프로브: 섹션이 1개 이상인 종목만 본문 보유로 간주, revision_number로 변경 감지
    out_dir = KB_DIR / "wiki"
    probes: dict[str, tuple[int, int | None]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (code, n_sec, rev) in enumerate(ex.map(lambda it: probe_sections(api, it), items), 1):
            probes[code] = (n_sec, rev)
            if i % 500 == 0:
                log(f"  프로브 {i}/{len(items)}")
    with_body = [it for it in items if probes[it["stock_id"]][0] > 0]
    log(f"본문 보유 종목 {len(with_body)}건")

    todo, kept = [], 0
    for it in with_body:
        p = out_dir / f"{it['stock_id']}.json"
        rev = probes[it["stock_id"]][1]
        if p.exists():
            try:
                old = load(p)
                if rev is not None and old.get("wiki_revision") == rev and old.get("description") == it.get("description"):
                    kept += 1
                    continue
            except Exception:  # noqa: BLE001
                pass
        todo.append(it)
    # 본문이 사라진 종목의 옛 레코드는 제거 (목록에 없는 코드 포함)
    live = {it["stock_id"] for it in with_body}
    if not limit:
        for p in out_dir.glob("*.json"):
            if p.stem not in live:
                p.unlink()
    log(f"위키 갱신 대상 {len(todo)}건 (변경 없음 {kept}건)")
    records: list[dict] = []
    fails: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(build_wiki_record, api, it): it for it in todo}
        for i, f in enumerate(as_completed(futs), 1):
            it = futs[f]
            try:
                rec = f.result()
                dump(out_dir / f"{rec['stock_id']}.json", rec)
                records.append(rec)
            except Exception as exc:  # noqa: BLE001
                fails.append(f"{it['stock_id']} {it.get('name')}: {exc}")
            if i % 50 == 0:
                log(f"  위키 {i}/{len(todo)}")
    if fails:
        log(f"위키 실패 {len(fails)}건:\n  " + "\n  ".join(fails[:20]))
    return records


# ──────────────────────────── 테마 ────────────────────────────
def compact_stock(s: dict) -> dict:
    return {
        "stock_id": s.get("stock_id"),
        "name": s.get("company_name_kr") or s.get("raw_stock_name"),
        "market": s.get("ticker_market") or s.get("source_market"),
        "rank": s.get("rank"),
        "is_leader": bool(s.get("is_leader")),
        "membership_score": s.get("membership_score"),
        "rs": s.get("rs_score"),
        "return_1d": s.get("return_1d"),
        "return_5d": s.get("return_5d"),
        "return_ytd": s.get("return_ytd"),
        "market_cap_krw": s.get("market_cap_krw"),
        "current_price": s.get("current_price"),
        "source_tags": s.get("source_tags") or {},
    }


def build_theme_record(api: AntWikiAPI, t: dict) -> dict:
    d = api.theme_detail(t["categorySlug"], t["slug"])
    th = d.get("theme") or {}
    cat = d.get("category") or {}
    stocks = sorted((compact_stock(s) for s in d.get("stocks", [])), key=lambda s: (s["rank"] is None, s["rank"] or 0))
    leaders = [s for s in stocks if s["is_leader"]]
    return {
        "slug": th.get("slug") or t["slug"],
        "name": th.get("display_name") or t["name"],
        "category_slug": cat.get("slug") or t["categorySlug"],
        "category_name": cat.get("display_name") or t["categoryName"],
        "description": th.get("description") or t.get("description"),
        "etf": {"code": th.get("etf_code"), "name": th.get("etf_name") or th.get("etf_label")},
        "metrics": {
            "return_1d": th.get("return_1d"), "return_5d": th.get("return_5d"), "return_ytd": th.get("return_ytd"),
            "rs": th.get("rs_score"), "short_rs": th.get("short_rs_score"), "long_rs": th.get("long_rs_score"),
            "heat": th.get("heat_score"), "market_cap_krw": th.get("market_cap_krw"),
        },
        "as_of": {
            "rs_trade_date": th.get("rs_as_of_trade_date"),
            "expected_trade_date": (d.get("meta") or {}).get("expectedTradeDate"),
            "snapshot_at": (stocks[0].get("snapshot_at") if stocks and stocks[0].get("snapshot_at") else None),
        },
        "stock_count": th.get("stock_count") or len(stocks),
        "leaders": [s["stock_id"] for s in leaders],
        "stocks": stocks,
        "related_themes": [{"slug": r.get("slug"), "name": r.get("display_name"), "category_id": r.get("category_id")} for r in d.get("relatedThemes", [])],
        "url": theme_url(cat.get("slug") or t["categorySlug"], th.get("slug") or t["slug"]),
    }


def build_themes(api: AntWikiAPI, limit: int | None, workers: int) -> tuple[list[dict], list[dict]]:
    idx = api.theme_wiki_index()
    cats = idx.get("categories", [])
    themes_meta = idx.get("themes", [])
    log(f"산업(카테고리) {len(cats)}개, 테마 {len(themes_meta)}개")
    dump(KB_DIR / "theme_wiki_index.json", idx)

    # 산업 레코드 (밸류체인 포함)
    cat_records = []
    for c in cats:
        try:
            d = api.theme_category(c["slug"])
        except Exception as exc:  # noqa: BLE001
            log(f"  산업 {c['slug']} 실패: {exc}")
            d = {}
        cat = d.get("category") or {}
        cat_records.append({
            "slug": c["slug"], "name": c["name"], "description": c.get("description"),
            "mid_group": cat.get("mid_group"), "narrative": cat.get("narrative"),
            "value_chain": cat.get("value_chain"),
            "metrics": {k: cat.get(k) for k in ("return_1d", "return_5d", "return_ytd", "rs_score", "heat_score", "theme_count", "stock_count", "market_cap_krw")},
            "themes": [{"slug": t["slug"], "name": t["name"]} for t in themes_meta if t.get("categorySlug") == c["slug"]],
        })
        dump(KB_DIR / "categories" / f"{c['slug']}.json", cat_records[-1])

    if limit:
        themes_meta = themes_meta[:limit]
    records: list[dict] = []
    fails: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(build_theme_record, api, t): t for t in themes_meta}
        for i, f in enumerate(as_completed(futs), 1):
            t = futs[f]
            try:
                rec = f.result()
                dump(KB_DIR / "themes" / f"{rec['slug']}.json", rec)
                records.append(rec)
            except Exception as exc:  # noqa: BLE001
                fails.append(f"{t['categorySlug']}/{t['slug']}: {exc}")
            if i % 50 == 0:
                log(f"  테마 {i}/{len(themes_meta)}")
    if fails:
        log(f"테마 실패 {len(fails)}건:\n  " + "\n  ".join(fails[:20]))
    return cat_records, records


# ──────────────────────────── 인덱스 / brief ────────────────────────────
def _fmt_pct(v) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _fmt_cap(v) -> str:
    if not v:
        return "—"
    jo = v / 1e12
    return f"{jo:.1f}조" if jo >= 1 else f"{v/1e8:,.0f}억"


# 위키 본문 매칭에서 제외할 일반명사형 종목명 (kb.query.STOP_NAMES와 동일 취지)
_MENTION_STOP = {
    "한국", "대한", "현대", "삼성", "동양", "국제", "세계", "미래", "우리", "중앙", "서울", "경방", "대상", "삼양", "동아", "한화", "한솔", "코리아", "글로벌", "유니온",
    "에이치", "케이", "비엠", "디", "에스", "지", "씨", "브이", "케이티", "남성", "상보", "전방", "채비", "한창", "국전", "서한", "서연", "서남", "디오", "우진", "일성",
    "동성", "대성", "신성", "경동", "진로", "성문", "보성", "대원", "한일", "한양", "동부", "대교", "신한", "선진", "동방", "광명", "대유", "삼일", "제일", "한진", "모두",
    "이수", "인지", "원익", "태양", "조광", "국보", "대동", "대호", "성호", "유성", "경인", "동원", "부광", "진양", "SK", "LG", "GS", "CJ", "DL", "LS", "HD현대", "두산", "효성", "코오롱", "롯데", "현대차",
}
_BOUND = "가-힣A-Za-z0-9"


def build_mention_index(wiki: dict[str, dict], names: dict[str, str]) -> dict[str, list[dict]]:
    """모든 위키 섹션 텍스트에서 다른 상장사 이름을 찾아 역색인. 짧은 이름(≤3자)은 어절 경계 필수."""
    cands = {code: nm for code, nm in names.items() if nm and len(nm) >= 2 and nm not in _MENTION_STOP and not re.search(r"(우|우B|스팩\d*|\d우|\(전환\))$", nm)}
    # 긴 이름 우선 alternation. 3자 이하는 경계 lookaround 포함
    by_len = sorted(cands.items(), key=lambda kv: -len(kv[1]))
    parts = []
    for code, nm in by_len:
        esc = re.escape(nm)
        if len(nm) <= 3:
            parts.append(rf"(?<![{_BOUND}]){esc}(?![{_BOUND}])")
        else:
            parts.append(esc)
    big = re.compile("|".join(parts))
    name_to_code = {nm: code for code, nm in cands.items()}
    index: dict[str, list[dict]] = {}
    sent_split = re.compile(r"(?<=[.!?다음함임됨])\s+|\n+")
    pref_rx = re.compile(r"(우|우B|\d우|스팩\d*)$")
    for wcode, w in wiki.items():
        if pref_rx.search(w.get("name") or ""):
            continue  # 우선주·스팩 위키는 본주와 중복 내용 → 언급 주체에서 제외
        seen_pairs: set[tuple[str, str]] = set()
        for sec in w.get("sections", []):
            text = sec.get("text") or ""
            if not text:
                continue
            for sent in sent_split.split(text):
                sent = sent.strip(" -*")
                if len(sent) < 8:
                    continue
                for m in big.finditer(sent):
                    code = name_to_code.get(m.group(0))
                    if not code or code == wcode:
                        continue
                    key = (code, sec.get("heading") or "")
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    index.setdefault(code, []).append({
                        "wiki": wcode, "name": w.get("name"), "heading": sec.get("heading") or "개요",
                        "sentence": sent[:240],
                    })
    # 위키별 1건으로 압축: 관계를 직접 서술하는 섹션(타사와의 관계·비즈니스 모델·고객·공급·지분) 문장을 우선 선택
    pri = ("타사와의 관계", "비즈니스 모델", "고객", "공급", "협력", "지분", "주주", "개요")
    def _pri(r: dict) -> int:
        h = r["heading"]
        for i, k in enumerate(pri):
            if k in h:
                return i
        return len(pri)
    for code, rows in index.items():
        best: dict[str, dict] = {}
        for r in rows:
            cur = best.get(r["wiki"])
            if cur is None or _pri(r) < _pri(cur):
                best[r["wiki"]] = r
        index[code] = sorted(best.values(), key=_pri)[:400]
    return index


def build_indexes_and_briefs() -> dict:
    names: dict[str, str] = load(KB_DIR / "stock_names.json") if (KB_DIR / "stock_names.json").exists() else {}
    summary: dict[str, dict] = load(KB_DIR / "stocks_summary.json") if (KB_DIR / "stocks_summary.json").exists() else {}
    for code, sm in summary.items():
        if sm.get("name"):
            names.setdefault(code, sm["name"])
    wiki = {p.stem: load(p) for p in sorted((KB_DIR / "wiki").glob("*.json"))}
    themes = {p.stem: load(p) for p in sorted((KB_DIR / "themes").glob("*.json"))}
    cats = {p.stem: load(p) for p in sorted((KB_DIR / "categories").glob("*.json"))}

    # name → code (전 상장종목 + 위키 영문명)
    name_to_code: dict[str, str] = {}
    for code, nm in names.items():
        name_to_code.setdefault(normalize_name(nm), code)
    for code, w in wiki.items():
        name_to_code.setdefault(normalize_name(w.get("name", "")), code)
        if w.get("english_name"):
            name_to_code.setdefault(normalize_name(w["english_name"]), code)
    for t in themes.values():
        for s in t["stocks"]:
            if s.get("name") and s.get("stock_id"):
                name_to_code.setdefault(normalize_name(s["name"]), s["stock_id"])

    theme_stocks = {slug: [s["stock_id"] for s in t["stocks"]] for slug, t in themes.items()}
    stock_themes: dict[str, list[dict]] = {}
    for slug, t in themes.items():
        for s in t["stocks"]:
            stock_themes.setdefault(s["stock_id"], []).append({
                "slug": slug, "name": t["name"], "category_slug": t["category_slug"], "category_name": t["category_name"],
                "is_leader": s["is_leader"], "rank": s["rank"],
            })
    dump(KB_DIR / "index" / "name_to_code.json", name_to_code)
    dump(KB_DIR / "index" / "theme_stocks.json", theme_stocks)
    dump(KB_DIR / "index" / "stock_themes.json", stock_themes)
    # 테마명/별칭 → slug (뉴스 매칭용). 전체 이름과 slug는 항상, 부분어는 "그 부분어를 가진 테마가 하나뿐일 때만"
    theme_aliases: dict[str, str] = {}
    part_owner: dict[str, set[str]] = {}
    generic = {"반도체", "소재", "장비", "부품", "서비스", "플랫폼", "에너지", "건설", "금융", "바이오", "제약", "기타", "전공정", "후공정", "생산", "유통", "저장", "대형", "중소", "중견", "종합",
               "글로벌", "산업용", "모바일", "콘솔", "브랜드", "인프라", "솔루션", "시스템", "디지털", "스마트", "온라인", "대표", "관련", "수혜", "국내", "해외", "개발", "제조", "기업", "전자", "통신", "전력", "자동차", "식품", "의류", "화학", "기계", "운송", "교육", "게임", "미디어", "방송", "여행", "항공", "조선", "해운", "철강", "은행", "증권", "보험", "리츠", "유틸리티", "플렉서블", "폴더블", "롤러블"}
    for slug, t in themes.items():
        theme_aliases[normalize_name(t["name"])] = slug
        theme_aliases[normalize_name(slug)] = slug
        for part in re.split(r"[·/()（）,\s]+", t["name"]):
            pn = normalize_name(part)
            if len(pn) >= 3 and pn not in generic:
                part_owner.setdefault(pn, set()).add(slug)
    for pn, owners in part_owner.items():
        if len(owners) == 1 and pn not in theme_aliases:
            theme_aliases[pn] = next(iter(owners))
    dump(KB_DIR / "index" / "theme_aliases.json", theme_aliases)

    # ── 위키 본문 기업명 언급 역색인 ──
    # mention_index[언급된 회사코드] = [{"wiki": 언급한 위키 종목코드, "name", "heading", "sentence"}]
    # 용도: 기사 주체 기업(예 SK하이닉스)이 나오면 그 회사를 본문에서 언급한 위키(예 한미반도체 "SK하이닉스와의 깊은 협력")를
    #       tier 1/2 후보 + 근거 문장으로 즉시 제공. LLM 기억 대신 위키 문장으로 공급관계를 뒷받침한다.
    mention_index = build_mention_index(wiki, names)
    dump(KB_DIR / "index" / "mention_index.json", mention_index)

    # ── brief 1: 산업-테마 지도 ──
    as_of = next((t["as_of"].get("rs_trade_date") for t in themes.values() if t.get("as_of")), None)
    lines = [
        "# 앤트위키 산업-테마 지도",
        f"(생성 {datetime.now(KST):%Y-%m-%d %H:%M} KST · 테마 RS/등락률 기준일 {as_of} · 산업 {len(cats)}개 · 테마 {len(themes)}개)",
        "",
        "표기: 테마명 [slug] — 1D/5D 등락률 · RS · 종목수 · 대장주(★) 및 주요 종목(순위순, 최대 6).",
        "종목은 `이름(코드)`. 수치는 api.ant.wiki에서 읽은 값이며 기준일을 함께 표기할 것.",
        "",
    ]
    for cslug, c in sorted(cats.items(), key=lambda kv: kv[1].get("name", "")):
        m = c.get("metrics") or {}
        lines.append(f"## {c['name']} [{cslug}] — 1D {_fmt_pct(m.get('return_1d'))} · 5D {_fmt_pct(m.get('return_5d'))} · RS {m.get('rs_score', '—')} · 테마 {m.get('theme_count', len(c['themes']))}개 · 종목 {m.get('stock_count', '—')}개")
        if c.get("narrative"):
            lines.append(f"설명: {c['narrative']}")
        vc = c.get("value_chain") or {}
        if vc.get("stages"):
            stage_txt = " → ".join(
                f"{st.get('label')}(" + ", ".join((it.get("label", "") + ("🔥" if it.get("hot") else "")) for it in st.get("items", [])) + ")"
                for st in vc["stages"]
            )
            lines.append(f"밸류체인: {stage_txt}")
        if vc.get("trends"):
            lines.append("트렌드/응용: " + ", ".join((it.get("label", "") + ("🔥" if it.get("hot") else "")) for it in vc["trends"]))
        lines.append("")
        for tm in c["themes"]:
            t = themes.get(tm["slug"])
            if not t:
                continue
            mm = t["metrics"]
            tops = t["stocks"][:6]
            stock_txt = ", ".join(("★" if s["is_leader"] else "") + f"{s['name']}({s['stock_id']})" for s in tops)
            etf = f" · ETF {t['etf']['name']}" if t.get("etf", {}).get("name") else ""
            lines.append(f"- {t['name']} [{t['slug']}] — 1D {_fmt_pct(mm.get('return_1d'))} · 5D {_fmt_pct(mm.get('return_5d'))} · RS {mm.get('rs', '—')} · {t['stock_count']}종목{etf} :: {stock_txt}")
        lines.append("")
    (KB_DIR / "brief").mkdir(parents=True, exist_ok=True)
    (KB_DIR / "brief" / "industry_theme_map.md").write_text("\n".join(lines), encoding="utf-8")

    # ── brief 2: 종목 핵심 요약 (위키 보유 종목) ──
    lines = [
        "# 앤트위키 종목 핵심 요약 (위키 보유 종목)",
        f"(생성 {datetime.now(KST):%Y-%m-%d %H:%M} KST · {len(wiki)}종목 · 출처 api.ant.wiki 종목 위키 infobox/한줄설명)",
        "",
        "형식: `### 이름(코드) · 시장 · 업종` / 한줄 설명 / 소속 테마(★대장) / 투자포인트 / 리스크 / 위키 섹션 제목",
        "",
    ]
    for code, w in sorted(wiki.items(), key=lambda kv: kv[1].get("name", "")):
        th = stock_themes.get(code) or [
            {"name": t["name"], "is_leader": t["is_leader"]} for t in w.get("themes", [])
        ]
        th_txt = ", ".join(("★" if t.get("is_leader") else "") + t["name"] for t in th[:10]) or "—"
        lines.append(f"### {w['name']}({code}) · {w.get('exchange') or '—'} · {w.get('industry') or '—'}")
        desc = w.get("one_liner") or w.get("description") or ""
        if desc:
            lines.append(f"- 설명: {desc}")
        if w.get("description") and w.get("one_liner") and w["description"] != w["one_liner"]:
            lines.append(f"- 부제: {w['description']}")
        lines.append(f"- 테마: {th_txt}")
        if w.get("tags"):
            lines.append(f"- 태그: {', '.join(w['tags'][:12])}")
        for ip in (w.get("investment_points") or [])[:2]:
            lines.append(f"- 투자포인트: {ip}")
        for rf in (w.get("risk_factors") or [])[:2]:
            lines.append(f"- 리스크: {rf}")
        heads = [s["heading"] for s in w.get("sections", []) if s.get("heading")]
        if heads:
            lines.append(f"- 위키 섹션: {' / '.join(heads[:12])}")
        lines.append("")
    (KB_DIR / "brief" / "stocks_core.md").write_text("\n".join(lines), encoding="utf-8")

    # ── brief 3: 종목 인덱스 (전 종목 한 줄, 항상 적재용) ──
    lines = [
        "# 앤트위키 종목 인덱스 (전 상장종목 한 줄 설명)",
        f"(생성 {datetime.now(KST):%Y-%m-%d %H:%M} KST · {len(summary)}종목 · [W]=종목 위키 본문 있음 · ★=테마 대장주)",
        "",
    ]
    for code, sm in sorted(summary.items(), key=lambda kv: (kv[1].get("name") or "")):
        th = stock_themes.get(code, [])
        th_txt = ", ".join(("★" if t.get("is_leader") else "") + t["name"] for t in th[:6])
        flag = " [W]" if code in wiki else ""
        desc = (wiki.get(code, {}).get("one_liner") if code in wiki else None) or sm.get("description") or ""
        if not desc and not th:
            continue  # 설명도 테마도 없는 종목(우선주 등)은 생략
        lines.append(f"- {sm.get('name')}({code}){flag}: {desc}" + (f" — 테마: {th_txt}" if th_txt else ""))
    (KB_DIR / "brief" / "stocks_index.md").write_text("\n".join(lines), encoding="utf-8")

    manifest = {
        "built_at": datetime.now(KST).isoformat(),
        "stock_names": len(names), "wiki": len(wiki), "themes": len(themes), "categories": len(cats),
        "theme_metrics_as_of": as_of,
        "brief_bytes": {
            "industry_theme_map.md": (KB_DIR / "brief" / "industry_theme_map.md").stat().st_size,
            "stocks_core.md": (KB_DIR / "brief" / "stocks_core.md").stat().st_size,
            "stocks_index.md": (KB_DIR / "brief" / "stocks_index.md").stat().st_size,
        },
        "stocks_summary": len(summary),
    }
    dump(KB_DIR / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-wiki", action="store_true")
    ap.add_argument("--skip-themes", action="store_true")
    ap.add_argument("--brief-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    t0 = time.time()
    if not args.brief_only:
        api = AntWikiAPI()
        try:
            log("종목명 매핑 수집")
            dump(KB_DIR / "stock_names.json", api.stock_names())
            if not args.skip_themes:
                build_themes(api, args.limit, args.workers)
            if not args.skip_wiki:
                build_wiki(api, args.limit, args.workers)
        finally:
            api.close()
    log("인덱스·brief 생성")
    m = build_indexes_and_briefs()
    log(f"완료 ({time.time() - t0:.0f}s): {json.dumps(m, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
