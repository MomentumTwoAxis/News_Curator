# 앤트위키 뉴스 큐레이터 — 기획안 (v0.1, 2026-10-12)

## 1. 목표

뉴스를 수급해 **앤트위키 지식(종목 위키·산업-테마 구조)을 먼저 인지한 상태에서** 수혜주를 분석하고,
결과를 앤트위키 웹사이트(피드)에 게시하는 파이프라인을 만든다.

```
[1] 뉴스 수급  →  [2] 수혜주 분석 (앤트위키 KB 선인지 + 필요 시 검색)  →  [3] 웹사이트 게시
```

핵심 차별점은 2단계다. 기존 텔레그램 수혜주 봇은 뉴스를 보고 나서 테마 DB를 "조회"하지만,
이 서비스는 분석 전에 앤트위키의 **산업 19개 → 테마 252개 → 종목 1,754개(엣지 2,929개)** 구조와
**종목 위키 2,651건**(개요·투자포인트·리스크·내러티브, 그중 사람이 편집한 문서 약 1,061건)을 컨텍스트로 적재한 뒤 뉴스를 읽는다.
그래서 분석 결과가 앤트위키의 분류 체계·용어·대장주 판단과 일치하고, 글에 위키/테마 링크를 자연스럽게 연결할 수 있다.

## 2. 이미 있는 자산 (재사용 대상)

| 자산 | 위치 | 재사용 포인트 |
| --- | --- | --- |
| 앤트위키 공개 REST API (FastAPI, OpenAPI 공개) | `https://api.ant.wiki/openapi.json` (448 paths) | 종목 위키·테마·시세 **읽기**. 인증 불필요 |
| 종목 위키 전체 | `GET /api/v1/wiki/list?order_by=name`, `/wiki/{code}`, `/wiki/{code}/sections` | 2,670 상장종목 중 2,651에 본문 존재. ProseMirror doc + infobox(투자포인트·리스크·PER/PBR 등) + 내러티브 |
| 산업-테마-종목 구조 | `/theme-page/wiki-index`, `/theme-page/categories/{c}`, `/theme-page/categories/{c}/themes/{t}` | 19 카테고리(밸류체인 단계 포함) → 252 테마 → 구성종목(대장주·RS·등락률) |
| 종목↔테마 역색인 | `/theme-page/stocks/{code}/themes` | 종목이 속한 테마와 형제 종목 |
| 종목코드↔이름 | `/market/stock-names` | 전 상장종목 2,700+ |
| 피드 발행 게이트웨이 | `antwiki-feed` CLI (`~/.local/bin`), `POST /api/v1/agent/publish` | **운영 환경(api.ant.wiki)**, actor `momentum`, surface `feed_post` 준비 완료 |
| 수혜주 분석 봇 (텔레그램) | `../antwiki_telegram_bot_v2/antwiki_beneficiary_bot` | RSS 23개 + Naver 검색 API 수집, Gemini 4-step 분석, 중복 제거 로직 |
| 테마 블로그 워크플로 | `../Naver_Blog_Writer_테마수급/CLAUDE.md` | "수치는 앤트위키에서 직접 읽은 값만", 재서술·출처 링크 원칙, 발행 로그 CSV 패턴 |
| 테마 밸류체인 마스터(수기) | `../앤트위키_테마/*.md` | 반도체·2차전지 세부 분류 보강 자료 |

## 3. 아키텍처

```
┌──────────────── Stage 0. 지식베이스(KB) 빌드 — 일 1회(장 마감 후) ────────────────┐
│ kb/build_kb.py                                                                    │
│  api.ant.wiki → data/kb/wiki/{code}.json  (2,651 위키: 개요·섹션 텍스트·투자포인트·리스크·태그) │
│               → data/kb/themes/{slug}.json (252 테마: 구성종목·대장주·RS·등락률)         │
│               → data/kb/categories/{slug}.json (19 산업: 밸류체인 단계·트렌드)            │
│               → data/kb/index/*.json      (이름↔코드, 종목→테마, 테마→종목 역색인)      │
│               → data/kb/brief/industry_theme_map.md  ← 분석 시 항상 적재 (산업-테마 지도, 72KB) │
│               → data/kb/brief/stocks_index.md        ← 전 종목 한 줄 인덱스(334KB, 캐싱 시 적재)   │
│               → data/kb/brief/stocks_core.md         ← 위키 보유 종목 상세(2.3MB, kb.query로 발췌) │
└───────────────────────────────────────────────────────────────────────────────────┘
                                   │
┌──────────────── Stage 1. 뉴스 수급 — 속보형, 5~10분 주기(장중) ─────────────────────┐
│ pipeline/collect_telegram.py  ★주요 소스: 텔레그램 속보 채널 (Telethon, sources.yaml telegram.channels) │
│ pipeline/collect.py           보조 소스: RSS(경제·산업 전문지) + Naver 뉴스 검색 API(옵션)          │
│  → 신규 기사 → data/news/{date}.jsonl                                                       │
│  0토큰 프리스크린: 제목 정규식(정치인 발언·수상·CSR·프로모션·재건축·행정절차·연예) → 제외          │
│  1차 필터: 중복(URL/제목), 제외 키워드(광고·시황·특징주), 상장종목명/테마명 포함 여부      │
└───────────────────────────────────────────────────────────────────────────────────┘
                                   │
┌──────────────── Stage 2. 수혜주 분석 — 기사별 ────────────────────────────────────┐
│ kb/query.py → 기사에서 언급 종목·테마를 KB에서 매칭 → "컨텍스트 팩" 생성             │
│   + 위키 언급 역색인(mention_index): 주체 기업을 본문에서 언급한 위키 = tier1/2 후보+근거 문장 │
│   + specific_segment → 테마 라우팅(--segment): 후보 풀을 세그먼트 테마로 제한(다른 사업부 차단) │
│   (항상) 산업-테마 지도  (+ 캐싱 가능하면 종목 인덱스)                                   │
│   (기사별) 매칭 테마의 구성종목·대장주·RS, 매칭 종목의 위키 섹션·투자포인트·리스크          │
│ LLM 분석 (prompts/analyze_news.md)                                                 │
│   1) 뉴스 유형·영향 경로  2) 수혜주/피해주 + 근거(위키 문장 인용)  3) 확신도·검증            │
│   KB에 근거가 부족하면 → 웹 검색으로 보강(출처 URL 기록) — KB 우선, 검색은 보조           │
│ 출력: data/analysis/{date}/{id}.json (구조화) + 게시용 본문                            │
└───────────────────────────────────────────────────────────────────────────────────┘
                                   │
┌──────────────── Stage 3. 웹사이트 게시 ──────────────────────────────────────────┐
│ pipeline/publish.py → antwiki-feed check(dry-run) → [사람 승인] → publish (+poll 옵션)   │
│ pipeline/track.py   → 발행 수혜주 D+1/5/20 수익률 추적(/stock/{code}/prices) → 월간 적중률 글 │
│  post: body_text(≤4000자) · stance · stock_ids(≤10) · primary_stock_id                │
│  scenario: pipeline="news_curator_v1", content_type="article_relay", source_event_ids │
│  idempotency_key = news-curator-{YYYYMMDD}-{기사해시}  → 발행 로그 data/published.csv   │
└───────────────────────────────────────────────────────────────────────────────────┘
```

### 분석 LLM 실행 방식 (2가지)
- **반자동(1차)**: Claude Code가 `CLAUDE.md` 워크플로대로 KB 적재 → 기사 선택 → 분석 → 초안 → 승인 후 발행. 테마 블로그 프로젝트와 같은 운영 방식. 비용·키 관리 없이 바로 시작 가능.
- **자동(2차)**: `pipeline/analyze.py`에서 LLM API 호출로 동일 프롬프트 실행 → 스케줄러(Railway/Cloud Run Job, 기존 봇과 동일 패턴). 1차에서 프롬프트·품질 기준이 굳어진 뒤 전환.

## 4. 데이터 계약

### 4-1. KB 종목 레코드 `data/kb/wiki/{code}.json`
`stock_id, name, description(한줄), industry, exchange, tags[], investment_points[], risk_factors[], valuation{per,pbr,eps,bps,dividend_yield,foreign_ratio}, sections[{heading, text}], narratives{bullish[], bearish[]}, themes[{slug,name,category,is_leader,rank}], updated_at`

### 4-2. KB 테마 레코드 `data/kb/themes/{slug}.json`
`slug, name, category_slug, category_name, description, etf{code,name}, metrics{return_1d,return_5d,return_ytd,rs,short_rs,heat}, stocks[{stock_id,name,market,rank,is_leader,rs,return_1d,market_cap_krw,source_tags}], related_themes[slug], as_of`

### 4-3. 분석 결과 `data/analysis/{date}/{id}.json`
```json
{"news": {"title":"", "url":"", "source":"", "published_at":""},
 "news_type": "정책|수주계약|실적|M&A|기술|공급망|규제|거시", "impact_path": "…",
 "themes": [{"slug":"", "name":"", "relation":"direct|indirect"}],
 "beneficiaries": [{"stock_id":"", "name":"", "score":0.0, "role":"대장|2등|소재|장비",
                    "reason":"", "kb_evidence":["위키 문장…"], "web_evidence":[{"url":"","note":""}]}],
 "losers": [], "confidence": 0.0, "caveats": [], "post": {"body_text":"", "stance":"", "stock_ids":[], "primary_stock_id":""}}
```

## 5. 게시 원칙 (테마 블로그 운영 규칙 승계)
- 수치는 앤트위키 API에서 읽은 값만 사용. 기억·추정 금지. 기사 문장은 복사 금지, 재서술 + 출처 링크.
- "매수 추천/목표가" 금지. 재료가 불명확하면 "확인되지 않았다"로 서술.
- 글 말미 면책: "본 글은 정보 제공 목적이며 투자 판단과 책임은 본인에게 있습니다."
- 앤트위키 내부 링크: 관련 테마 페이지(`/themes/{category}/{theme}`) + 대장주 위키(`/wiki/{종목명}-{코드}`).
- **발행은 실제 외부 쓰기(운영 피드)**. 초기에는 반드시 `check` → 사람 승인 → `publish`. 자동 발행 전환은 별도 합의.

## 5-0. 로컬 웹 (2026-10-12 추가)
`web/server.py` — 앤트위키 서비스 디자인(헤더·Pretendard·820px 중앙·좌우 레일)을 따른 로컬 페이지. 뉴스 리스트 → 클릭 시 우리 분석 + 원문 이동, 상단에 링크 분석 입력. 분석은 `pipeline/analyze.py`(Anthropic 우선, Gemini 폴백)로 자동 실행.

## 5-0-1. 주가 영향력 지표 (2026-10-12 추가)
`score = 100 × (0.6 × 펀더멘털 + 0.4 × 반응)`. 펀더멘털 = 0.5×핵심사업 관련성 + 0.5×매출·이익 영향(LLM, 위키 비즈니스 모델·규모 대비 판단). 반응 = 뉴스 전후 1~3분 변동 절댓값(%)을 0.3→0.2, 1→0.5, 2→0.8, 3+→1.0으로 사상(키움 1분봉 ka10080). 장외 뉴스는 직전 종가→다음 세션 09:03, 마감 동시호가(15:20~15:30)는 15:30 단일가 기준. 미측정이면 펀더멘털만.

## 5-1. 2026-10-12 결정 사항 (텔레그램 봇 분석 후)
- **속보형으로 간다.** 하루 3~5건 장문이 아니라 봇처럼 빠른 주기로 수혜주 글을 낸다. 글 길이는 속보용(짧게)과 심화용(위키 근거)을 분리.
- **텔레그램 채널이 주요 뉴스 소스.** RSS·Naver는 보조. 세션 구독 채널 전체(47개, 단톡방·채팅방 제외) 등록 (`collect_telegram.py`, `sources.yaml telegram.channels`).
- 테마 단일화(봇의 테마 DB를 앤트위키로 교체)는 **하지 않음**. 우리는 앤트위키 테마 API만 쓴다.
- 채택: 위키 언급 역색인(구현), specific_segment 라우팅(구현), 성과 추적(구현), poll 실험(발행 번들 지원), 0토큰 정규식 프리스크린(구현).
- 속보형 전환의 함의: 분석 단계를 Claude Code 수동에서 **LLM API 자동(`pipeline/analyze.py`, 구현됨)** 으로 옮김. 현재 자격증명은 Gemini(봇 키 공유)만 있어 Gemini로 동작하며, ANTHROPIC_API_KEY를 두면 Claude(claude-opus-5, 산업지도 prompt caching)로 전환됨. 사람 승인은 "자동 발행 + 사후 모니터링"으로 완화하는 시점을 별도 합의.

## 6. 단계별 일정 (제안)
| 단계 | 산출물 | 완료 기준 |
| --- | --- | --- |
| M0 (오늘, 완료) | 기획안, KB 빌더, 수집·발행 골격 | `python -m kb.build_kb` 성공(위키 2,651·테마 252·산업 19), brief 3종 생성, 수집 dry-run 통과 |
| M1 | 반자동 분석 워크플로 (Claude Code) | 뉴스 5건 → 분석 JSON → 초안 → TEST 피드 1건 발행 readback |
| M2 | 자동 분석(`analyze.py`) + 중복 제거 + 품질 게이트 | 하루 자동 실행, 승인 큐에 초안 적재 |
| M3 | 스케줄러 배포(Railway/Cloud Run) + 발행 로그/모니터링 | 주 5일 무인 운영, 오류 알림 |

## 7. 리스크·열린 결정
1. **발행 대상 surface**: 지금 actor `momentum`은 `feed_post`만 허용. 종목 위키 "뉴스/히스토리" 섹션이나 `stock_narrative`에 쓰려면 운영자에게 surface 추가 요청 필요.
2. **TEST 환경 먼저**: 현재 CLI는 PROD로 설정됨. M1 검증은 `api-test.ant.wiki`(client `team_agent_test_01`) 크리덴셜을 따로 받아 진행 권장.
3. **뉴스 소스 라이선스**: RSS 제목·요약과 링크만 사용, 본문 전문 저장·게시 금지. Naver 검색 API는 키(`NAVER_CLIENT_ID/SECRET`) 필요 — 기존 봇과 공유 여부 결정.
4. **KB 신선도**: 테마 지표(`servingGeneration`)가 10-01 기준으로 11일 지연 상태였음(응답 meta `freshnessStatus: stale`). 글에 RS·등락률을 쓸 때는 `as_of`를 함께 표기.
5. **기존 봇과의 중복**: 텔레그램 봇이 같은 뉴스를 다루므로, 큐레이터는 "하루 N건 엄선 + 위키 근거 중심 장문"으로 포지셔닝해 차별화. 봇의 `sent_analysis_log`를 조회해 중복 소재 회피 가능(Supabase 접근 필요).
6. **LLM 비용**: 항상 적재하는 것은 산업-테마 지도(72KB)만. 종목 인덱스(334KB)는 프롬프트 캐싱이 가능할 때만 상시 적재하고, 그 외에는 `kb.query` 컨텍스트 팩(매칭된 테마·종목만)으로 대체.
