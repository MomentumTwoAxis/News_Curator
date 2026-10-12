"""뉴스 1건에 대한 앤트위키 컨텍스트 팩 생성.

사용:
  python -m kb.query "기사 제목. 기사 요약/본문 …"            # 마크다운 컨텍스트 팩 출력
  python -m kb.query --json "…"                                 # 매칭 결과 JSON
  python -m kb.query --file article.txt

동작:
  1) 본문에서 상장 종목명(name_to_code) · 테마명/별칭(theme_aliases) · 위키 태그를 문자열 매칭
  2) 매칭 종목 → 소속 테마 확장 (형제 종목 = 후보 수혜주)
  3) 매칭 테마 → 구성 종목·대장주·RS·등락률
  4) 위키 보유 종목이면 개요/투자포인트/리스크/관련 섹션 발췌
출력된 컨텍스트 팩 + brief 2종(항상 적재)을 LLM 프롬프트(prompts/analyze_news.md)에 넣는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "data" / "kb"

# 흔한 일반명사와 겹치는 종목명은 단독 매칭에서 제외 (오탐 방지)
STOP_NAMES = {
    "한국", "대한", "현대", "삼성", "동양", "국제", "세계", "미래", "우리", "중앙", "서울", "경방", "대상", "삼양", "동아", "한화", "한솔", "코리아", "글로벌", "유니온",
    "에이치", "케이", "비엠", "디", "에스", "지", "씨", "브이", "케이티",
    # 일반명사와 같은 짧은 종목명 (뉴스 문장에서 단독 어절로 자주 등장) — 코드로 직접 언급될 때만 다루기로 함
    "남성", "상보", "전방", "채비", "한창", "국전", "서한", "서연", "서남", "디오", "우진", "일성", "동성", "대성", "신성", "경동", "진로", "성문", "보성", "대원", "한일", "한양", "동부", "대교", "신한", "선진", "동방", "광명", "대유", "우리들", "삼일", "제일", "한진", "모두", "에스원", "이수", "마니커", "인지", "원익", "태양", "조광", "국보", "대동", "대호", "성호", "유성", "경인", "동원", "부광", "파미", "진양",
}
STOP_NAMES |= {"디바이스", "서산", "유신", "e8", "모두투어", "케어", "바이오", "에너지", "글로벌", "미디어", "솔루션", "시스템", "테크", "파워", "네트웍스", "로보"}



def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def normalize(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


_URL_RX = re.compile(r"https?://\S+|www\.\S+|\b[\w.-]+\.(?:com|co\.kr|kr|net|me|io|ai)(?:/\S*)?", re.I)


def normalize_loose(s: str) -> str:
    """URL/도메인을 제거하고 공백은 하나로만 접어 어절 경계를 보존한다 (naver.me 링크가 'NAVER' 종목으로 잡히는 오탐 방지)."""
    s = _URL_RX.sub(" ", s or "")
    return re.sub(r"\s+", " ", s.lower()).strip()


# 짧은 이름(정규화 ≤ SHORT_LEN)은 앞뒤가 한글/영숫자가 아닐 때만 매칭 → '하이브리드'에서 '하이브', '서남권'에서 '서남' 차단
SHORT_LEN = 3
_BOUNDARY_CLASS = "가-힣A-Za-z0-9"


_PAT_CACHE: dict[tuple, re.Pattern] = {}


def _short_pattern(token: str, allow_trailing_digits: bool = False) -> re.Pattern:
    """토큰 내부 공백은 0~1칸 허용. allow_trailing_digits=True면 'hbm'이 'hbm4'·'hbm3e'에도 매칭(테마 별칭용)."""
    key = (token, allow_trailing_digits)
    if key not in _PAT_CACHE:
        inner = r"\s?".join(re.escape(ch) for ch in token)
        tail = "가-힣A-Za-z" if allow_trailing_digits else _BOUNDARY_CLASS
        _PAT_CACHE[key] = re.compile(rf"(?<![{_BOUNDARY_CLASS}]){inner}(?![{tail}])")
    return _PAT_CACHE[key]


class KB:
    def __init__(self, kb_dir: Path = KB_DIR):
        self.dir = kb_dir
        self.name_to_code: dict[str, str] = _load(kb_dir / "index" / "name_to_code.json")
        self.theme_aliases: dict[str, str] = _load(kb_dir / "index" / "theme_aliases.json")
        self.stock_themes: dict[str, list[dict]] = _load(kb_dir / "index" / "stock_themes.json")
        self.stock_names: dict[str, str] = _load(kb_dir / "stock_names.json")
        self.summary: dict[str, dict] = _load(kb_dir / "stocks_summary.json") if (kb_dir / "stocks_summary.json").exists() else {}
        for code, sm in self.summary.items():
            if sm.get("name"):
                self.stock_names.setdefault(code, sm["name"])
        self.manifest = _load(kb_dir / "manifest.json") if (kb_dir / "manifest.json").exists() else {}
        mi = kb_dir / "index" / "mention_index.json"
        self.mention_index: dict[str, list[dict]] = _load(mi) if mi.exists() else {}
        self._theme_cache: dict[str, dict] = {}
        self._wiki_cache: dict[str, dict | None] = {}
        # 긴 이름 우선 매칭을 위해 정렬
        self._names_sorted = sorted((n for n in self.name_to_code if len(n) >= 2 and n not in STOP_NAMES), key=len, reverse=True)
        self._aliases_sorted = sorted((a for a in self.theme_aliases if len(a) >= 2), key=len, reverse=True)

    def theme(self, slug: str) -> dict | None:
        if slug not in self._theme_cache:
            p = self.dir / "themes" / f"{slug}.json"
            self._theme_cache[slug] = _load(p) if p.exists() else None
        return self._theme_cache[slug]

    def wiki(self, code: str) -> dict | None:
        if code not in self._wiki_cache:
            p = self.dir / "wiki" / f"{code}.json"
            self._wiki_cache[code] = _load(p) if p.exists() else None
        return self._wiki_cache[code]

    # ── 매칭 ──
    @staticmethod
    def _consume(loose: str, token: str, theme: bool = False) -> tuple[str, bool]:
        """token(정규화형)을 loose 텍스트에서 찾고, 찾으면 그 자리를 공백으로 지워(마스킹)
        더 짧은 이름이 부분 문자열로 재매칭되는 것을 막는다. 예) 'sk하이닉스' → '하이닉스'·'이닉스'·'sk' 차단.
        짧은 토큰(≤SHORT_LEN)은 어절 경계가 있어야 매칭."""
        if len(token) <= SHORT_LEN or (theme and token.isascii()):
            pat = _short_pattern(token, allow_trailing_digits=theme)
        else:
            key = (token, None)
            if key not in _PAT_CACHE:
                _PAT_CACHE[key] = re.compile(r"\s?".join(re.escape(ch) for ch in token))
            pat = _PAT_CACHE[key]
        m = pat.search(loose)
        if not m:
            return loose, False
        return loose[: m.start()] + " " * (m.end() - m.start()) + loose[m.end():], True

    def match(self, text: str, max_themes: int = 6, max_stocks: int = 12, min_theme_alias_len: int = 2) -> dict:
        loose = normalize_loose(text)
        stocks: dict[str, dict] = {}
        for n in self._names_sorted:  # 긴 이름부터
            if len(stocks) >= max_stocks:
                break
            loose, hit = self._consume(loose, n)
            if hit:
                code = self.name_to_code[n]
                if code in stocks:
                    continue
                stocks[code] = {"stock_id": code, "name": self.stock_names.get(code) or n, "matched_by": "name", "has_wiki": (self.dir / "wiki" / f"{code}.json").exists()}
        themes: dict[str, dict] = {}
        loose_t = normalize_loose(text)  # 테마는 종목명과 겹쳐도 되므로 원문 기준(단, 테마끼리는 마스킹)
        for a in self._aliases_sorted:
            if len(themes) >= max_themes:
                break
            if len(a) < min_theme_alias_len:
                continue
            loose_t, hit = self._consume(loose_t, a, theme=True)
            if hit:
                slug = self.theme_aliases[a]
                if slug not in themes:
                    themes[slug] = {"slug": slug, "matched_by": f"alias:{a}", "alias_len": len(a)}
        # 종목 → 테마 확장 (직접 언급 종목이 속한 테마)
        expanded: dict[str, dict] = {}
        for code in stocks:
            for t in self.stock_themes.get(code, []):
                if t["slug"] not in themes and t["slug"] not in expanded:
                    expanded[t["slug"]] = {"slug": t["slug"], "matched_by": f"stock:{code}", "is_leader_stock": t.get("is_leader")}
        return {"stocks": list(stocks.values()), "themes": list(themes.values()), "expanded_themes": list(expanded.values())[: max_themes]}

    # ── specific_segment → 테마 라우팅 ──
    def route_segment(self, segment: str, max_themes: int = 4) -> list[str]:
        """'HBM4 고대역폭메모리' 같은 세그먼트 문구를 테마 slug로 매핑. 별칭 사전 → 토큰 매칭 순."""
        if not segment:
            return []
        hits = [t["slug"] for t in self.match(segment, max_themes=max_themes, max_stocks=0)["themes"]]
        if hits:
            return hits
        toks = [normalize(t) for t in re.split(r"[\s·/(),]+", segment) if len(t) >= 2]
        for a in self._aliases_sorted:
            if any(a in t or t in a for t in toks if len(t) >= 3):
                hits.append(self.theme_aliases[a])
                if len(hits) >= max_themes:
                    break
        return list(dict.fromkeys(hits))

    _REL_HEADINGS = ("타사와의 관계", "비즈니스 모델", "고객", "공급", "협력", "지분", "주주")
    _REL_WORDS = ("공급", "납품", "고객사", "협력", "파트너", "지분", "최대주주", "계열", "자회사", "모회사", "수주", "계약")

    def mentions_of(self, code: str, limit: int = 12, prefer_themes: list[str] | None = None) -> list[dict]:
        """code 기업을 본문에서 언급한 위키 목록(공급·협력 관계 후보 + 근거 문장).
        순위: ① 세그먼트 테마(prefer_themes)에 속한 위키 ② 관계 서술 섹션/문장 ③ 테마 대장주 ④ 테마 수."""
        rows = self.mention_index.get(code, [])
        pref = set(prefer_themes or [])
        def rank(r):
            th = self.stock_themes.get(r["wiki"], [])
            in_seg = 0 if pref and any(t["slug"] in pref for t in th) else 1
            rel_h = 0 if any(k in r["heading"] for k in self._REL_HEADINGS) else 1
            rel_w = 0 if any(k in r["sentence"] for k in self._REL_WORDS) else 1
            leader = 0 if any(t.get("is_leader") for t in th) else 1
            return (in_seg, rel_h + rel_w, leader, -len(th))
        ranked = sorted(rows, key=rank)
        # 관계 단어도 없고 세그먼트도 아닌 단순 언급은 뒤로 밀리므로 limit 안에서 자연히 탈락
        return ranked[:limit]

    # ── 컨텍스트 팩 ──
    def context_pack(self, text: str, section_hint_terms: list[str] | None = None, segment: str | None = None) -> str:
        """segment(specific_segment)가 주어지면 '언급 종목이 속한 테마' 확장을 세그먼트 테마로만 제한한다 (다른 사업부 오염 차단)."""
        m = self.match(text)
        seg_slugs = self.route_segment(segment) if segment else []
        if seg_slugs:
            m["segment_themes"] = [{"slug": s, "matched_by": f"segment:{segment}"} for s in seg_slugs if s not in {t["slug"] for t in m["themes"]}]
            m["expanded_themes"] = [t for t in m["expanded_themes"] if t["slug"] in seg_slugs]
        else:
            m["segment_themes"] = []
        out: list[str] = []
        as_of = self.manifest.get("theme_metrics_as_of")
        out.append(f"# 앤트위키 컨텍스트 팩 (KB 빌드 {self.manifest.get('built_at', '?')[:16]} · 테마 지표 기준일 {as_of})")
        if segment:
            out.append(f"specific_segment: {segment} → 라우팅 테마 {seg_slugs or '없음(산업 지도에서 추론)'}")
        out.append("")
        if not m["stocks"] and not m["themes"]:
            out.append("_본문에서 앤트위키 종목명·테마명이 직접 매칭되지 않음. 산업-테마 지도(brief)에서 영향 경로를 추론하고, 필요하면 웹 검색으로 보강._")
            return "\n".join(out)

        if m["stocks"]:
            out.append("## 본문에 언급된 상장 종목")
            for s in m["stocks"]:
                w = self.wiki(s["stock_id"])
                th = self.stock_themes.get(s["stock_id"], [])
                th_txt = ", ".join(("★" if t.get("is_leader") else "") + t["name"] for t in th[:8]) or "—"
                out.append(f"### {s['name']}({s['stock_id']}) — 테마: {th_txt}")
                if w:
                    out.append(f"- 위키: {w['url']}")
                    desc = w.get("one_liner") or w.get("description")
                    if desc:
                        out.append(f"- 설명: {desc}")
                    for ip in (w.get("investment_points") or [])[:2]:
                        out.append(f"- 투자포인트: {ip}")
                    for rf in (w.get("risk_factors") or [])[:2]:
                        out.append(f"- 리스크: {rf}")
                    v = w.get("valuation") or {}
                    if v.get("per") or v.get("pbr"):
                        out.append(f"- 밸류에이션: PER {v.get('per') or '—'} · PBR {v.get('pbr') or '—'} · 배당 {v.get('dividend_yield') or '—'}% · 외인 {v.get('foreign_ratio') or '—'}% (기준 {str(w.get('valuation_as_of') or '')[:10]})")
                    # 관련 섹션 발췌: 힌트 단어 포함 섹션 우선, 없으면 개요
                    terms = [normalize(t) for t in (section_hint_terms or [])] + [a.split(":", 1)[1] for a in (t["matched_by"] for t in m["themes"]) if ":" in a]
                    picked = [sec for sec in w.get("sections", []) if sec["text"] and any(t and t in normalize(sec["heading"] + sec["text"]) for t in terms)]
                    if not picked:
                        picked = [sec for sec in w.get("sections", []) if sec["text"]][:1]
                    for sec in picked[:2]:
                        body = sec["text"][:700].replace("\n", " ")
                        out.append(f"- 위키 섹션 「{sec['heading'] or '개요'}」: {body}")
                    bl = w.get("narratives", {}).get("bullish") or []
                    br = w.get("narratives", {}).get("bearish") or []
                    if bl:
                        out.append(f"- 커뮤니티 긍정 내러티브: {bl[0]['text'][:200]}")
                    if br:
                        out.append(f"- 커뮤니티 부정 내러티브: {br[0]['text'][:200]}")
                else:
                    sm = self.summary.get(s["stock_id"]) or {}
                    if sm.get("description"):
                        out.append(f"- 설명(앤트위키 한줄): {sm['description']}")
                    out.append("- 종목 위키 본문 없음 (한줄 설명·테마 구성정보만 사용)")
                rel = self.mentions_of(s["stock_id"], prefer_themes=seg_slugs or [t["slug"] for t in m["themes"]])
                if rel:
                    out.append(f"- 이 회사를 위키 본문에서 언급한 종목 (공급·협력 관계 후보, tier 1/2 근거):")
                    for r in rel:
                        th = ", ".join(("★" if t.get("is_leader") else "") + t["name"] for t in self.stock_themes.get(r["wiki"], [])[:3]) or "—"
                        out.append(f"  · {r['name']}({r['wiki']}) [{th}] 「{r['heading']}」: {r['sentence'][:160]}")
                out.append("")

        for title, key in (("## 본문에 언급된 테마", "themes"), ("## specific_segment 라우팅 테마 (후보 수혜주 풀)", "segment_themes"), ("## 언급 종목이 속한 테마 (후보 수혜주 풀)" + (" — 세그먼트로 제한됨" if seg_slugs else ""), "expanded_themes")):
            if not m[key]:
                continue
            out.append(title)
            for tm in m[key]:
                t = self.theme(tm["slug"])
                if not t:
                    continue
                mm = t["metrics"]
                out.append(f"### {t['name']} [{t['slug']}] · {t['category_name']} — 1D {mm.get('return_1d'):+.1f}% · 5D {mm.get('return_5d'):+.1f}% · RS {mm.get('rs')} · {t['stock_count']}종목 (매칭: {tm['matched_by']})" if mm.get("return_1d") is not None else f"### {t['name']} [{t['slug']}] · {t['category_name']} (매칭: {tm['matched_by']})")
                out.append(f"- 테마 페이지: {t['url']}")
                if t.get("description"):
                    out.append(f"- 설명: {t['description']}")
                rows = []
                for s in t["stocks"][:12]:
                    tag = "★" if s["is_leader"] else ""
                    r1 = f"{s['return_1d']:+.1f}%" if s.get("return_1d") is not None else "—"
                    rows.append(f"{tag}{s['name']}({s['stock_id']}) RS{s.get('rs') or '—'} 1D {r1}" + (" [위키]" if (self.dir / "wiki" / f"{s['stock_id']}.json").exists() else ""))
                out.append("- 구성종목(순위순): " + " · ".join(rows))
                if t.get("related_themes"):
                    out.append("- 연관 테마: " + ", ".join(r["name"] for r in t["related_themes"][:6]))
                out.append("")
        return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("text", nargs="?", help="기사 제목+본문")
    ap.add_argument("--file", help="기사 텍스트 파일")
    ap.add_argument("--json", action="store_true", help="매칭 결과만 JSON으로")
    ap.add_argument("--segment", help="specific_segment (예: 'HBM4 고대역폭메모리') — 후보 풀을 이 세그먼트 테마로 제한")
    ap.add_argument("--mentions", help="종목코드 — 이 회사를 언급한 위키 목록만 출력")
    args = ap.parse_args(argv)
    text = Path(args.file).read_text(encoding="utf-8") if args.file else (args.text or ("" if args.mentions else sys.stdin.read()))
    kb = KB()
    if args.mentions:
        print(json.dumps(kb.mentions_of(args.mentions, limit=50), ensure_ascii=False, indent=1))
    elif args.json:
        print(json.dumps(kb.match(text), ensure_ascii=False, indent=1))
    else:
        print(kb.context_pack(text, segment=args.segment))
    return 0


if __name__ == "__main__":
    sys.exit(main())
