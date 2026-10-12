# 앤트위키 뉴스 큐레이터 (News Curator)

뉴스를 수급해 **앤트위키 지식(종목 위키 + 산업·테마·종목 구조)을 먼저 적재한 상태에서** 수혜주를 분석하고, 결과를 로컬 웹과 앤트위키 피드에 내보내는 파이프라인입니다.
2026-10-12 기준 M1.5 상태: 수집·분석·점수화·로컬 웹까지 동작하고, 피드 발행은 사람 승인 후 CLI로만 합니다.

```
[텔레그램 47채널 · RSS] → 수집(0토큰 필터·중복 묶기) → KB 컨텍스트 팩 → LLM 분석(3단 논리·수혜주·펀더멘털)
   → 주가 영향력(키움 1분봉) → 로컬 웹(앤트위키 디자인) → [승인] → 앤트위키 피드 발행 → 성과 추적(D+1/5/20)
```

관련 문서: [docs/PLAN.md](docs/PLAN.md)(기획·결정 이력), [CLAUDE.md](CLAUDE.md)(운영 규칙·화면 규칙), [docs/고도화_아이디어_from_텔레그램봇.md](docs/고도화_아이디어_from_텔레그램봇.md), [prompts/analyze_news.md](prompts/analyze_news.md)(분석 지시문과 출력 JSON 계약).

---

## 1. 빠른 시작

```bash
cd News_Curator
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python 3.12 기준

# 1) 지식베이스 빌드 (앤트위키 공개 API, 인증 없음, 전체 ~7분 / 증분 ~3분, data/kb 약 70MB)
.venv/bin/python -m kb.build_kb

# 2) 뉴스 수집
.venv/bin/python -m pipeline.collect_telegram --dry-run   # 텔레그램 (주요 소스, TG_* 자격증명 필요)
.venv/bin/python -m pipeline.collect --dry-run            # RSS (보조)

# 3) 분석 (LLM) + 주가 영향력
.venv/bin/python -m pipeline.analyze --pending --workers 4
.venv/bin/python -m pipeline.impact update

# 4) 로컬 웹
.venv/bin/uvicorn web.server:app --reload --port 8765     # http://localhost:8765

# 또는 2~3을 한 번에, 주기 실행
.venv/bin/python -m pipeline.loop --interval 10
```

### 자격증명 (값은 어디에도 커밋하지 않음)

| 용도 | 변수 | 읽는 순서 |
| --- | --- | --- |
| 텔레그램 수집 (Telethon 사용자 세션) | `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING` | 환경변수 → `./.env` → `../antwiki_telegram_bot_v2/.env` |
| LLM 분석 | `ANTHROPIC_API_KEY`(우선) 또는 `GEMINI_API_KEY`, 선택 `LLM_PROVIDER=anthropic|gemini`, `ANTHROPIC_MODEL`(기본 claude-opus-5), `GEMINI_MODEL`(기본 gemini-2.5-flash) | 동일 |
| 주가 영향력 (키움 REST 1분봉) | `APP_KEY`, `APP_SECRET`, `KIWOOM_MODE=real|demo` | 환경변수 → `./.env` → `~/.claude/mcp-servers/kiwoom-exec-mcp/credentials.env` |
| 피드 발행 | `antwiki-feed` CLI (`~/.local/bin`), 크리덴셜 `~/.config/antwiki-feed/credentials.json` | CLI가 직접 읽음 |
| Naver 뉴스 검색 (선택) | `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 환경변수 |

현재 이 Mac에는 Anthropic 키가 없어 Gemini로 동작합니다. Anthropic 키를 두면 자동으로 Claude(산업-테마 지도 prompt caching)로 전환됩니다.

---

## 2. 디렉터리

```
kb/                      지식베이스
  api.py                 앤트위키 공개 API 클라이언트 (https://api.ant.wiki/api/v1, OpenAPI: /openapi.json)
  pm_text.py             위키 본문(ProseMirror JSON) → 섹션 텍스트
  build_kb.py            KB 빌드: 위키 2,651건·테마 252·산업 19·종목 2,670 + 인덱스 + brief
  query.py               기사 텍스트 → 종목/테마 매칭 → LLM용 "컨텍스트 팩"
pipeline/
  sources.yaml           텔레그램 채널 47개, RSS 9개, 0토큰 제외 규칙, 신호어
  collect_telegram.py    Stage 1  주요 소스 수집 (Telethon)
  collect.py             Stage 1  RSS/Naver 수집 + 공용 필터·중복 묶기
  analyze.py             Stage 2  LLM 분석 (Anthropic SDK 우선, Gemini 폴백) → data/analysis/{date}/{id}.json
  impact.py              Stage 2b 주가 영향력 점수 (펀더멘털 + 키움 1분봉 반응)
  publish.py             Stage 3  앤트위키 피드 발행 (antwiki-feed check → --live 승인 발행) + 성과 추적 등록
  track.py               발행 수혜주 D+1/5/20 수익률 추적·적중률 리포트
  loop.py                수집 → 미분석 자동 분석 주기 루프 (발행 미포함)
web/
  server.py              FastAPI: 리스트 / 상세 / 링크 분석 API / 이미지 프록시
  enrich.py              og:image, 실시간 시세, 1M·1D 스파크라인, 테마 지표, 본문 블록 파싱
  templates/, static/    앤트위키 디자인 셸(헤더 52px·Pretendard·820px 콘텐츠·좌우 레일)
prompts/analyze_news.md  분석 지시문 + 출력 JSON 계약
data/                    (git 제외) kb/ news/ analysis/ cache/ track/ published.csv
```

---

## 3. 데이터 흐름과 계약

### 3-1. KB (`data/kb/`)
| 파일 | 내용 | 용도 |
| --- | --- | --- |
| `wiki/{code}.json` | 종목 위키: 한줄설명, 투자포인트·리스크, 밸류에이션, 섹션 텍스트, 커뮤니티 내러티브, 소속 테마 | 종목별 근거 |
| `themes/{slug}.json` | 테마: 구성종목(순위·대장주·RS·등락률), ETF, 연관 테마, as_of | 후보 풀 |
| `categories/{slug}.json` | 산업: 밸류체인 단계·트렌드, 색상 | 영향 경로 추론 |
| `index/name_to_code.json` | 종목명(정규화)→코드 | 기사 매칭 |
| `index/theme_aliases.json` | 테마명/별칭→slug (부분어는 유일할 때만) | 테마 매칭·세그먼트 라우팅 |
| `index/mention_index.json` | **기업코드 → 그 기업을 본문에서 언급한 위키(종목·섹션·문장)** | 공급·협력 관계 근거 (tier 1/2) |
| `brief/industry_theme_map.md` | 산업→밸류체인→테마→대장주 지도 (72KB) | LLM 시스템 프롬프트에 항상 적재 |
| `brief/stocks_index.md`, `stocks_core.md` | 전 종목 한 줄 / 위키 상세 | 필요 시 grep·검색 |

증분 빌드: `/wiki/{code}/sections`의 `revision_number`가 바뀐 종목만 다시 받습니다. `order_by=view_count` 정렬은 페이지 간 중복이 생기므로 `name` 정렬을 씁니다.

### 3-2. 수집 레코드 (`data/news/{YYYYMMDD}.jsonl`)
`{id, title, summary, url, source, source_type(telegram|general_rss|specialized_rss|naver|manual), published_at, collected_at, match{stocks[], themes[], expanded_themes[]}, also_reported_by[]}`

채택 기준: (1) 기간 내 (2) 제목 제외어·정규식 9종(정치인 발언·수상·CSR·프로모션·재건축·바이오 행정절차·인사·노무/연예·게시판) 통과 (3) 상장 종목명 1개 이상, 또는 3글자 이상 테마명 + 제목 신호어 (4) 제목 토큰 Jaccard ≥0.35·공통 2개면 같은 재료로 묶어 1건만.

### 3-3. 분석 JSON (`data/analysis/{date}/{id}.json`)
```json
{"id":"", "skip":false, "news":{"title","url","source","published_at","summary"},
 "news_type":"실적|수주/공급계약|정책/규제|M&A/지분|기술/제품|공급망/원자재|거시/환율/금리|기타",
 "impact_path":"", "themes":[{"slug","name","relation":"direct|indirect"}],
 "beneficiaries":[{"stock_id","name","role":"대장|2등|소재|장비|간접","score":0~1,"reason",
                   "logic":{"news_meaning","stock_profile","effect"}, "kb_evidence":[], "web_evidence":[{"url","note"}]}],
 "losers":[], "watchlist":[], "confidence":0~1, "caveats":[],
 "impact":{"core_business":0~1,"pnl_effect":0~1,"rationale","fundamental":{...},"reaction":{...},"score":0~100,"basis"},
 "post":{"body_text","stance":"bullish|bearish|neutral|question","stock_ids":[],"primary_stock_id","poll"},
 "meta":{"analyzed_at","provider","model","input_tokens","cache_read"}}
```
수혜 점수 0.9는 "직접 당사자 또는 위키 문장으로 공급·지분 관계가 확인된 종목"에만 줍니다. `logic`은 3단(뉴스의 의미 → 회사 설명 → 영향, 각 1~2문장)입니다.

### 3-4. 주가 영향력 (`impact.score`, 0~100)
```
score = 100 × (0.6 × 펀더멘털 + 0.4 × 반응)
펀더멘털 = 0.5 × core_business + 0.5 × pnl_effect           ← LLM이 위키 비즈니스 모델·규모로 판단
반응     = 뉴스 전후 1~3분 변동 절댓값(%) → 0.3%:0.2, 1%:0.5, 2%:0.8, 3%+:1.0 ← 키움 ka10080 1분봉
```
- 장중: 발행 직전 1분봉 종가 대비 +1/+2/+3분 종가 변동과 고저 범위의 최댓값. 15:20~15:30 동시호가는 15:30 단일가.
- 장외: 직전 세션 종가 → 다음 세션 09:01~09:03. 아직 개장 전이면 `reaction.status="개장 대기"`, 점수는 펀더멘털만(`basis=fund_only`). 상세 페이지를 열 때 자동 재계산됩니다.
- LLM 값이 없는 옛 분석은 뉴스 유형·확신도로 추정(`fundamental.source="estimated"`).

### 3-5. 발행 번들 (피드 v2 계약)
`post{body_text≤4000, stance, stock_ids≤10, primary_stock_id, poll, attachments}` + `scenario{pipeline:"news_curator_v1", content_type:"article_relay", source_event_ids}`. idempotency key는 `news-curator-{YYYYMMDD}-{id}`. 발행 로그 `data/published.csv`, 성과 추적 `data/track/records.jsonl`.

---

## 4. 로컬 웹 (http://localhost:8765)

| 경로 | 내용 |
| --- | --- |
| `GET /` | 뉴스 리스트(썸네일·종목/수혜주 태그·영향력 배지), 필터(전체·★핵심·분석완료·미분석·텔레그램·RSS), 상단 "뉴스 링크 분석" 입력 |
| `GET /news/{id}` | 히어로(og:image·원문 이동·게시 본문 복사·다시 분석) → 지표 타일 5개 → 관련 테마 카드 → **핵심 수혜주 큰 카드**(로고·시세·1M↔1D 차트(뉴스 시점 마커)·3단 논리·주가 영향력 상세·근거) → 수혜 후보 행 → 피해/관심 → 유의 |
| `POST /api/analyze {url, segment?}` | 본문 추출 → KB → LLM → 저장 (백그라운드, `/api/jobs/{id}`로 폴링) |
| `POST /api/analyze/{id}` | 수집 기사 재분석 |
| `GET /api/news`, `GET /api/analysis/{id}` | JSON |
| `GET /img?u=` | 외부 이미지 프록시(디스크 캐시) |

하이라이트(★ 핵심 뉴스): 수혜 점수 ≥0.9인 종목이 있고 주가 영향력 ≥90. 큰 카드는 점수 ≥0.9 종목만, 나머지는 간단 행.
정적 파일은 `?v=mtime`로 캐시 버스팅됩니다.

---

## 5. 운영 규칙 (반드시)

- **발행은 운영 피드에 대한 실제 쓰기**입니다. `antwiki-feed`는 PROD(api.ant.wiki, key `team_feed_prod_01`, actor `momentum`)로 설정돼 있습니다. `python -m pipeline.publish <json>`은 check(dry-run)까지만, `--live`는 사람이 승인한 글만.
- 수치는 앤트위키 API 값만 쓰고 기준일을 적습니다(등락률 당일 / 테마 RS는 `theme_metrics_as_of` / PER은 `valuation_as_of`). 기사 문장 복사 금지, 매수 추천·목표가 금지, 말미 면책 문구 고정.
- 시크릿은 `.env`·크리덴셜 파일에만. 로그·문서·커밋에 쓰지 않습니다.
- 수기 작성 분석과 LLM 분석이 같은 `id`면 덮어씁니다. 비교용은 `data/analysis_compare/`.
- 텔레그램은 사용자 세션(Telethon)입니다. 채널 목록은 `sources.yaml`에서 가감하고, `--list-dialogs`로 구독 채널을 확인합니다.

---

## 6. 알려진 이슈 · 다음 작업

1. **Gemini의 펀더멘털·수혜 점수가 후합니다.** 연예 기사(SBS)에 0.85, 테마 형제 종목을 0.6~0.7로 붙입니다. Claude 전환 또는 프롬프트의 tier 상한·예시 강화가 필요합니다. LLM JSON이 깨지면 1회 재시도합니다.
2. **테마 지표 지연**: 앤트위키 `servingGeneration`이 며칠 늦을 수 있습니다(`meta.freshnessStatus`). 페이지는 기준일을 함께 표시합니다.
3. **반응 측정 한계**: 발행 시각이 기사 작성 시각과 다를 수 있고(RSS), 텔레그램 전달 지연이 있습니다. 1분봉은 KRX 정규장만 봅니다.
4. **자동 발행 미구현**: 속보형 운영을 위해서는 "자동 발행 + 사후 모니터링" 전환 시점과 TEST 환경(api-test.ant.wiki) 검증이 필요합니다.
5. **스케줄러 미배치**: `pipeline.loop`는 로컬 프로세스입니다. Railway/Cloud Run Job 배포(텔레그램 봇과 같은 패턴)가 다음 단계입니다.
6. 매칭 오탐 방지용 종목명 불용어(`kb/query.py STOP_NAMES`)는 수동 목록입니다. 새 오탐이 보이면 추가하세요.
7. 성과 추적(`track.py`)은 발행이 시작돼야 데이터가 쌓입니다. 월간 적중률 글은 `track report`로 뽑습니다.

---

## 7. 외부 의존성 요약

| 대상 | 방식 | 비고 |
| --- | --- | --- |
| 앤트위키 읽기 | `https://api.ant.wiki/api/v1` 공개 REST | 위키·테마·시세·일봉·로고(`www.ant.wiki/stock-logo/{code}`) |
| 앤트위키 쓰기 | `antwiki-feed` CLI → `POST /api/v1/agent/publish` (HMAC) | surface `feed_post`만 허용 |
| 키움 REST | `POST /api/dostk/chart` ka10080, 토큰 `/oauth2/token` | 토큰 캐시 `data/cache/kiwoom_token.json`(0600) |
| LLM | Anthropic SDK (`claude-opus-5`, adaptive thinking, 1h 캐시) / google-genai | `pipeline/analyze.py` |
| 텔레그램 | Telethon StringSession | 봇 프로젝트와 세션 공유 |
| 뉴스 원문 | httpx + BeautifulSoup (og 태그·본문 블록) | 저장은 요약 1,500자·본문 8,000자까지 |
