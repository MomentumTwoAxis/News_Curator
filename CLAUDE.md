# News_Curator — 앤트위키 뉴스 큐레이터

> 역할: **뉴스 수급 → 수혜주 분석 → 앤트위키 웹사이트(피드) 게시** 파이프라인의 기획·개발·운영.
> 원칙: 뉴스를 보기 전에 앤트위키 지식(종목 위키 + 산업-테마-종목 구조)을 먼저 적재하고, 그 틀 안에서 분석한다. 웹 검색은 KB 보조용.
> 기획안: [docs/PLAN.md](docs/PLAN.md) · 분석 프롬프트: [prompts/analyze_news.md](prompts/analyze_news.md)

## 데이터 소스 (전부 공개 읽기 API, 인증 없음)
- `https://api.ant.wiki/api/v1` — OpenAPI: `https://api.ant.wiki/openapi.json`
- 종목 위키: `/wiki/list` → `/wiki/{code}` (infobox·본문·내러티브) · 테마: `/theme-page/wiki-index`, `/theme-page/categories/{c}/themes/{t}`, `/theme-page/stocks/{code}/themes`
- 웹 링크: 위키 `https://www.ant.wiki/wiki/{종목명}-{코드}` · 테마 `https://www.ant.wiki/themes/{category}/{theme}`

## 구조
```
kb/            지식베이스
  api.py         API 클라이언트         pm_text.py   ProseMirror→텍스트
  build_kb.py    KB 빌드 (python -m kb.build_kb)   query.py  기사별 컨텍스트 팩 (python -m kb.query "…" [--segment "HBM4"] / --mentions 000660)
                 index/mention_index.json = 위키 본문 기업명 언급 역색인(공급·협력 근거 문장)
pipeline/
  collect_telegram.py  Stage1 ★주요 소스: 텔레그램 속보 채널 (python -m pipeline.collect_telegram [--dry-run] [--list-dialogs]; TG_API_ID/HASH/SESSION_STRING)
  collect.py           Stage1 보조 소스: RSS/Naver (python -m pipeline.collect [--dry-run] [--hot-themes 5])   sources.yaml  소스/필터/정규식 프리스크린
  publish.py           Stage3 피드 게시 (python -m pipeline.publish <analysis.json> [--live]) — 발행 성공 시 track.record 자동
  track.py             성과 추적 (python -m pipeline.track update|report) — D+1/5/20 수익률
  impact.py            주가 영향력 점수 (python -m pipeline.impact update) — 60% 펀더멘털(LLM: core_business·pnl_effect) + 40% 실제 반응(키움 1분봉, 뉴스 전후 1~3분 변동 절댓값). 키움 자격증명은 ~/.claude/mcp-servers/kiwoom-exec-mcp/credentials.env 폴백
  analyze.py           Stage2 LLM 자동 분석 (python -m pipeline.analyze --url … | --pending) — LLM_PROVIDER=anthropic|gemini, 키는 .env 또는 ../antwiki_telegram_bot_v2/.env 폴백
  loop.py              속보 루프: 텔레그램→RSS 수집 후 미분석 건 자동 분석 (python -m pipeline.loop [--interval 10] [--once])
web/server.py        로컬 웹 (FastAPI): 뉴스 리스트·분석 상세·원문 이동·링크 분석. 실행 `.venv/bin/uvicorn web.server:app --reload --port 8765` (또는 .claude/launch.json "news-curator")
web/enrich.py        상세 보강: 기사 og:image, 종목 시세(/market/stocks)·1개월 스파크라인(/stock/{code}/prices), 테마/산업 지표, 영향경로·본문 블록 파싱. 캐시 data/cache/
web/templates, web/static   앤트위키 셸(헤더 52px·Pretendard·820px 콘텐츠·좌우 레일) 재현. 상세는 히어로 이미지·지표 타일·영향 경로 플로우·테마 카드·종목 카드(로고·시세·미니차트·점수·근거)·게시 초안 블록. 이미지는 /img 프록시(디스크 캐시)로 서빙
prompts/analyze_news.md   Stage2 분석 지시문 + 출력 JSON 계약
data/kb/       빌드 산출물 (wiki/ 2,651건, themes/ 252, categories/ 19, index/, brief/ 3종, manifest.json) ← git 제외, 약 70MB, 전체 빌드 ~7분·증분 빌드는 변경 위키만
data/news/{YYYYMMDD}.jsonl   수집 기사   data/analysis/{YYYYMMDD}/{id}.json   분석 결과   data/published.csv  발행 로그
```

## 반자동 워크플로 (M1) — "오늘 뉴스 큐레이션 진행해줘"
1. **KB 갱신**: `python3 -m kb.build_kb` (변경된 위키만 다시 받음, 테마는 전체 갱신). `data/kb/manifest.json`의 `theme_metrics_as_of` 확인 — 글의 수치 기준일로 쓴다.
2. **KB 적재**: `data/kb/brief/industry_theme_map.md`(72KB)를 읽어 산업→밸류체인→테마→대장주 구조를 인지한 상태로 시작한다 (뉴스보다 먼저). 종목 상세는 기사별 `kb.query` 컨텍스트 팩으로 받는다. `stocks_index.md`(전 종목 한 줄, 334KB)·`stocks_core.md`(위키 상세, 2.3MB)는 필요한 부분만 grep으로 본다.
3. **수집**: `python3 -m pipeline.collect --hot-themes 5` → 당일 jsonl. 채택 기사 중 주가 영향 가능성이 높은 것 3~5건을 고르고 선정 사유를 보고.
4. **분석**: 기사마다 먼저 specific_segment(예 "HBM4 고대역폭메모리")를 정하고 `python3 -m kb.query --segment "<세그먼트>" "<제목. 요약>"` 로 컨텍스트 팩을 만들고(역색인의 '이 회사를 언급한 종목'이 tier 1/2 후보), `prompts/analyze_news.md` 절차대로 분석 → `data/analysis/{YYYYMMDD}/{id}.json` 저장. 근거가 부족하면 웹 검색으로 보강하고 URL을 `web_evidence`에 남긴다.
5. **검수**: 수치·링크가 KB 값과 일치하는지, 금지 표현 없는지, 면책 문구 있는지 자가 점검 → `python3 -m pipeline.publish <json>` (check만).
6. **발행**: 사용자가 승인한 글만 `--live`. 발행 후 URL과 `data/published.csv` 기록을 보고.

## 로컬 웹 (M1.5)
- `/` 뉴스 리스트(텔레그램·RSS 수집분 + 링크 분석분), 상단 '뉴스 링크 분석' 입력 → `POST /api/analyze` → 백그라운드 LLM → `/news/{id}`.
- `/news/{id}` 우리 분석(영향 경로·테마·수혜주·위키/웹 근거·게시 초안) + '원문으로 이동'. 미분석이면 KB 컨텍스트 팩만 보여주고 '지금 분석' 버튼.
- 실행 환경은 `.venv` (fastapi·anthropic·telethon 포함). 의존성 `requirements.txt`.
- 수기 초안과 LLM 결과가 같은 id면 덮어쓰므로, 비교용 결과는 `data/analysis_compare/`에 둔다.

## 화면 규칙 (2026-10-12 사용자 지시)
- 상단 타일: 확신도 · 논조 · 수혜주 수 · 근거 수 · **주가 영향력**(뉴스 유형 타일은 제거, 유형은 배지로만).
- 수혜주는 **점수 0.9 이상만 큰 카드**(로고·시세·1개월 차트 + 3단 논리: ①뉴스의 의미 ②회사 설명 ③영향, 각 1~2문장). 0.9 미만은 간단 행.
- 3단 논리는 LLM `logic` 필드가 우선이고, 없으면 서버가 (영향 경로 / 위키 한줄설명 / 선정 이유)로 폴백 구성해 표시한다.
- 주가 영향력 상세(펀더멘털 두 막대 + 1~3분 반응)는 별도 섹션이 아니라 **대표 종목의 큰 카드 안, 3단 논리 아래**에 붙인다. '영향 경로'·'게시 초안' 섹션은 없음(초안은 히어로의 '게시 본문 복사' 버튼).
- 큰 카드의 미니차트는 **1M(앤트위키 일봉) ↔ 1D(키움 1분봉, 전일 종가 기준선)** 를 4초마다 자동 전환, 호버 시 정지, 클릭 시 즉시 전환.
- 정적 파일은 `?v=mtime` 캐시 버스팅(base.html `asset_v()`), 수정 후 새로고침만 하면 반영.
- 수기 초안 3건(셀트리온·한화오션·호남전력)은 LLM 재생성 대상에서 제외. LLM v1 결과는 `data/analysis_compare/v1_*.json`.

## 분석 규칙 (텔레그램 봇에서 이식)
- specific_segment를 좁게 잡고 그 밖 사업부 종목은 넣지 않는다 (셀트리온 비만치료제 기사에 바이오시밀러 관련주 금지).
- tier 1 직접관계 ≤2 · tier 2 세그먼트 밸류체인 공급사 ≤3 · tier 3 섹터 동반 ≤2(is_sector_event=true일 때만).
- 논조가 분석을 통제한다. 피해주는 ①원가 전가 구조 ②역수혜 가능성 ③2단계 이상 간접 중 하나라도 해당하면 제외.
- 수혜주 reason은 기사 고유 사건을 포함해야 한다("다른 뉴스에도 쓸 수 있는 문장"이면 다시). "~알려짐" 금지, 위키 문장으로 단언.
- `kb.query`가 뽑은 직접 언급 종목은 LLM 검증이 제거하지 않는다(protected).

## 운영 규칙
- **발행은 운영 피드(api.ant.wiki, actor `momentum`)에 대한 실제 외부 쓰기.** `--live`는 사용자 승인 없이 실행하지 않는다. TEST 환경 검증은 `api-test.ant.wiki` 크리덴셜을 따로 받아 진행.
- 수치는 KB(=앤트위키 API)에서 읽은 값만, 기준일과 함께. 기억·추정 금지. 기사 문장 복사 금지(재서술 + 출처 링크).
- 매수 추천·목표가 금지. 재료 불명확 시 "확인되지 않았다". 말미 면책 문구 고정.
- 같은 기사/같은 재료는 하루 1회. `data/published.csv`와 `data/news/seen_ids.json`이 중복 판단 기준.
- 시크릿(Naver API 키, antwiki-feed secret)은 환경변수/`~/.config/antwiki-feed/credentials.json`에만. 저장소·문서·로그에 쓰지 않는다.
- 기존 자산 참고: 텔레그램 수혜주 봇 `../antwiki_telegram_bot_v2/antwiki_beneficiary_bot` (수집·중복제거 로직), 테마 블로그 `../Naver_Blog_Writer_테마수급` (글쓰기 규칙), 피드 CLI 문서 `../Feed_Uploader/agent-publish-test-handoff-20260831/`.
