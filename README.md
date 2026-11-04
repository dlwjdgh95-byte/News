# 📰 매일 아침 경제·시사 뉴스 브리핑 자동화

매일 새벽, 경제·시사 뉴스를 자동 수집·중복제거·요약하여 **Miner 레포의 정적 리포트
페이지**로 전달하는 시스템입니다. 세 종류의 데이터 소스를 통합하고, 어떤 경우에도 최소
브리핑이 남도록 **결정론적 폴백**을 갖춥니다.

> **2026-08-15 — 전달 경로 변경.** 텔레그램(`@realneeewsbot`) 발송을 없애고 리포트
> 페이지의 첫 탭(`뉴스`)으로 옮겼습니다. 이 레포는 하루에 `briefs/<date>.md`(위키
> ingest 소스)와 `briefs/<date>.json`(페이지 IR) 두 파일을 남기고, 통합 루틴이 json을
> Miner로 복사합니다. 자세한 배경은 Miner의
> `docs/superpowers/specs/2026-08-15-report-page-pipeline-design.md`.

## 핵심 설계 원칙

1. **폴백 우선 (Fallback-first).** LLM·분석 없이도 각 RSS에서 최신 제목+링크를 모으는
   단순 경로(`news/fallback.py`)를 먼저 구축했습니다. 메인 파이프라인이 실패·타임아웃·
   빈 결과면 자동으로 폴백이 실행되어 **그날 브리핑이 반드시 남습니다**. 폴백 산출물에는
   `[폴백 모드]` 표기와 IR의 `degraded: true` 플래그가 붙어, 페이지가 이를 "조용한 날"이
   아니라 "고장난 날"로 렌더합니다. 반대로 수집은 됐는데 지표·보유 종목과 관련된 기사가
   0건인 **조용한 날**은 고장이 아닙니다 — 폴백 없이 0건 정상 브리핑(md에 "오늘은 지표와
   관련된 필수 뉴스가 없습니다", IR에 `quiet: true`, `degraded` 없음)을 남깁니다.
2. **공식 RSS/API만 사용.** HTML 스크래핑(클라우드 IP 차단·빈 결과 유발)에 의존하지
   않습니다. 모든 피드 URL은 사용 전 유효 XML인지 검증하고, 깨진 피드는 건너뜁니다
   (`news/http.py`의 `fetch_feed`). "조용한 실패"(200 OK + 빈 본문)도 감지합니다.
3. **시크릿은 환경변수로만.** 코드·프롬프트에 평문 비밀값을 두지 않습니다.
4. **상태는 레포 파일에 집중.** 백엔드는 무상태. 발송 기록은 `state/sent_log.json`에
   정규화 URL 기준으로 저장하여 재발송을 막습니다.

## 데이터 소스

| 태그 | 분야 | 소스 | 폴백 |
|------|------|------|------|
| **A** | 시사·정치·국제·한국 | Google News RSS(키 불필요) + Guardian API(선택) | — |
| **B** | 경제·증시 | NewsData.io(감성·business 분류, **하루 ~3크레딧**) | Guardian → Google News RSS |
| **C** | 크립토·핀테크(보조) | CoinDesk / Cointelegraph / Decrypt / Google News RSS | — |

NewsData.io는 무료 200크레딧/일 중 **요청 3회·약 30건**만 쓰도록
`NEWSDATA_MAX_REQUESTS`로 강제 제한하고, 동일 쿼리는 캐싱(`cache/`)합니다.

## 처리 파이프라인 (`news/pipeline.py`) — 지표 중심 (2026-11-04)

핵심 지표나 보유 종목을 움직이는 정치·경제 뉴스만 남긴다. 무관한 기사는 LLM 앞에서
결정론으로 걸러내고, LLM은 **최대 8건을 골라 한 줄 요약 + 지표 태그**만 단다.

```
수집(A·B·C 병렬, 소스 격리) → 정규화(공통 모델)
  → 사전 키워드 필터(연예·스포츠 제외, 라이프 포함)
  → 다단계 중복제거(URL 정규화 → 제목 셰글링 → 시간/개체 과병합 방지)
  → 관련도 채점(결정론): indicators·category·score 부착, 필수 분류에 안 맞으면 제외
     (남는 기사가 0건이면 조용한 날 → 0건 정상 브리핑, quiet: true)
  → 발송 이력 대조(sent_log)
  → 후보 풀 캡(점수순 20건 + 다양성 캡: 매체당 3, 클러스터당 2 — 최종 선별은 매체당 2)
  → 선별 + 한 줄 요약(LLM, 최대 8건: 제목·한 줄 요약·지표 태그·confidence)
  → 번역·정렬 → 아카이브 2종 커밋(briefs/<date>.md + briefs/<date>.json)
어느 단계든 실패 시(수집 0건 포함) → 폴백 경로 자동 전환
```

### 관련도 채점 (`news/relevance.py` + `news/indicator_keywords.json`)
- **지표 키**는 Miner `briefing/indicators.yaml`과 같다(fed_upper·ust10y·usdkrw 등 27개).
  키워드 규칙 하나가 여러 지표에 걸린다(예: 관세 → core_yoy·kr_exports·usdkrw).
- **분류(category):** central_bank(중앙은행 결정·발언 — 해외 중앙은행은 연준 키 대신 dxy) ·
  data(지표 발표·시세) · policy(재정·관세·전국 단위 선거·규제) · geopolitics(유가·환율·금리
  키워드를 함께 건드릴 때만) · holding(보유 종목: ETH·비트마인·로빈후드·유니드·AI전력·금 ETF,
  + 크립토 대리 지표로 BTC). 어디에도 안 맞으면 버린다.
- **매칭 규칙:** 영문은 영숫자 경계 + 대소문자 무시(제목식 대문자 `U.S. Dollar`·`Clarity Act`,
  전부 대문자 `NASDAQ`도 맞음). 소문자면 다른 흔한 낱말이 되는 약어(TIPS·HOOD·Fed·SEC·QT·QE·ISM·
  JOLTS·BOK·RBI·US)와 티커 ETH만 JSON `case_sensitive`로 표기 그대로 맞춘다. 한국어는 부분 문자열이되, 짧은 낱말은
  `^`(한글 음절 바로 뒤에선 안 맞음 — `^한은` ≠ 북한은·시한은)·`$`(한글이 바로 이어지면 안 맞음)
  표식으로 경계를 건다. 전역 `mask`(유가족·추경호·palm oil·gold medal·billion-dollar 등)와
  규칙별 `mask`(관세청 ⊃ 관세)는 매칭 전에 지우고(거래소 티커 표기 `NASDAQ:MU`도), `unless`(한은·
  해외 중앙은행 → 연준 키 금지)는 제목에 보이면 기사 전체에서, 스니펫에만 보이면 스니펫에서만
  규칙을 끈다. `context`는 기사 어딘가에 그 낱말이 있어야 규칙이 서게 한다(노동시장·구인·실업률 같은
  일반 낱말은 美·미국·연준 등 미국 맥락일 때만 claims).
- **점수(score)** = 지표 키워드 적중(제목 2·스니펫 1, 상한 8) + 보유 종목(제목 2·스니펫 1)
  + 복수 매체(관련 보도 ≥ 2, +1) + 공식·통신사(로이터·블룸버그·연합뉴스·FT·WSJ·중앙은행 등, +1)
  − 경과 시간(하루 −1, 최대 −2).
- **토큰 목표:** LLM 입력 ≈ 후보 20건 × (제목 + 스니펫 120자), 출력 ≈ 한 줄 요약 8개.
  env `NEWS_MAX_CANDIDATES`(20)·`NEWS_MAX_ITEMS`(8)·`NEWS_SNIPPET_CHARS`(120)로 조정한다.
  어제 다이제스트는 기본 끔(`NEWS_YESTERDAY_DIGEST_CHARS`로 켬).

### 공통 기사 모델 (모듈 간 계약, `news/model.py`)
제목 · 요약 · 출처링크 · 매체명 · 발행시각 · 카테고리 · 감성점수 · 소스태그(A/B/C) ·
신뢰도 · 핵심개체 · 원문제목 + 관련도(지표 키/필수 분류/점수) + 다운스트림 보강 필드
(한줄요약/왜중요/태그/근거/플래그 — 새 계약에선 한줄요약만 필수).

### 과병합 방지 (최우선 안전 규칙, `news/dedup.py`)
별개 기사를 하나로 합치는 것을 **절대 피합니다.** 애매하면 분리가 기본값입니다.
- **시간 인식:** 같은 주제라도 발행 시각이 12시간 이상 벌어지고 상태 변화 신호어
  (첫날·이후·후속·급락·철회·사과·번져 등)가 있으면 후속 보도로 보고 분리.
- **개체·버전 구별:** 핵심 개체/버전이 다르면 분리(예: Gemini Pro ≠ Gemini Flash).
  핵심 수치가 충돌하면(영업이익 10조 ≠ 12조) 둘 다 남기고 `conflicting-figures` 플래그.
- **병합 조건:** 정규화 URL 동일 **또는** (제목 Jaccard ≥ 0.6 + 발행시각 근접 + 핵심개체 일치).

### 요약 — 근거 강제 (`news/summarize.py`)
헤드라인만으로 추론하지 않고 제목+요약 스니펫만 인용합니다. 각 기사는
제목/한줄요약/지표 태그/confidence로 구조화하며(왜중요·함의·태그·근거는 선택),
출처에 귀속되지 않는 단정 표현은 `unsourced-claim`으로 플래그합니다. LLM 없이
돌 때는 점수 상위 8건을 고르고 한국어 기사는 제목을 그대로 한 줄 요약으로 씁니다(번역할 수
없는 외국어 기사는 한 줄 요약을 비우고 제목에 `(원문 미번역)`). LLM·에이전트가 `indicators`를
빈 목록 `[]`으로 주면 "지표 없음"으로 보고 결정론 태그를 지우고, 키를 생략하거나 유효 키가
하나도 없으면 결정론 태그를 유지합니다. **신뢰도가 낮거나 근거가 약한 기사는
아카이브(`briefs/*.md`)에 보관하되 페이지 IR(`briefs/*.json`)에서는 제외**합니다.

### 번역 표기
한국어가 아닌 기사는 번역 제목을 위에, 원문을 괄호로 병기:
`美 연준, 금리 동결 결정 (원문: Fed Holds Rates Steady)`

## 출력 형식 (한국어)
오늘의 헤드라인 3줄 → 경제·시장(핵심 뉴스 + **감성 기반 시장 분위기 한 줄**)
→ 시사·국제(중립 톤) → 크립토(보조) → 오늘 주목할 이벤트. 기사마다
`지표: ust10y, usdkrw` 줄과 그 바로 아래 `분류: central_bank` 줄이 붙는다(지표가 없으면
분류 줄만; Miner 위키 ingest가 `분류:` 표식을 읽는다). IR(`briefs/<date>.json`, schema 2)
항목에는 `indicators`·`category`가 더해지고(기존 필드 이름은 그대로), `items`는 **중요도
순**(에이전트가 적은 `selected` 순서, 휴리스틱이면 관련도 점수순)이다 — 소비자는 앞 N건을
그대로 쓴다. md는 소스별 섹션 배치를 유지한다. 관련 기사가 0건인 날은 IR 최상위에
`quiet: true` + 빈 `items`.

## 두 가지 실행 방식

**① 자율 일체형 (GitHub Actions / API 키 경로)** — 한 프로세스가 수집~발송을 모두 수행하며,
선별·요약 LLM 단계는 `ANTHROPIC_API_KEY`로 API를 호출합니다.

**② 하이브리드 (Claude 예약 세션 / 구독 모델)** — 신뢰성이 필요한 부분은 파이썬이 결정론적으로
처리하고, **선별 1회 + 요약 1회**만 리드 에이전트가 구독 모델로 수행합니다. 자세한 절차는
[`ORCHESTRATION.md`](ORCHESTRATION.md) 참고.

```bash
pip install -r requirements.txt

# 공통
python run.py --check       # 설정/시크릿 점검 (발송 안 함)
python run.py --dry-run     # 브리핑 생성하되 발송·상태기록 안 함
python run.py --fallback    # 결정론적 폴백 강제 실행

# ① 자율 일체형
python run.py               # 전체 파이프라인 + 발송 (실패 시 자동 폴백)

# ② 하이브리드
python run.py --prepare     # 수집·중복제거 → state/candidates.json
#   (리드 에이전트가 구독 모델로 선별+요약 → state/selection.json 작성)
python run.py --finalize    # 렌더 + 아카이브 (selection 없으면 휴리스틱 대체)
```

`run.py`는 데이터 문제로 인한 소프트 실패에도 폴백 아카이브를 시도한 뒤 **항상 exit 0**
으로 종료해, 스케줄러가 하드 실패로 처리하지 않게 합니다. `--dry-run`은 아카이브 기록과
`sent_log` 기록을 모두 건너뛰어 레포 상태를 변경하지 않습니다.

### 신뢰성 보장 (재점검 반영)
- **재선별 방지:** `sent_log`는 이름과 달리 재발송이 아니라 **재선별**을 막습니다 — 어제
  실은 기사를 오늘 다시 고르지 않게 합니다. 발송이 사라진 뒤로는 `persist`만으로 기록되며,
  예전의 `send and persist` 게이트를 그대로 뒀다면 매일 같은 기사가 올라왔을 것입니다.
- **과병합 방지 강화:** 발행 시각이 멀면(≥12h) 병합하지 않으며(시각 근접이 필수 조건), 한국어
  제목의 구별 개체(예: 코스피≠코스닥)도 분리 판정에 사용합니다.

## 시크릿 설정
`.env.example`를 참고해 환경변수를 설정합니다. **필수:** `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. **선택:** `NEWSDATA_API_KEY`, `GUARDIAN_API_KEY`,
`ANTHROPIC_API_KEY`(+ `ANTHROPIC_MODEL`, 기본 `claude-sonnet-4-6`).
`ANTHROPIC_API_KEY`가 있으면 1회 선별 + 1회 요약 LLM 단계가 활성화되고, 없으면
휴리스틱 선별 + 무추론 요약으로 동작합니다(브리핑은 여전히 발송됨).

## 스케줄 — 매일 07:30 KST
`.github/workflows/daily-briefing.yml`이 `cron: "30 22 * * *"`(전날 22:30 UTC =
당일 07:30 KST)로 실행합니다. 매 실행마다 레포를 새로 클론하고, 끝에
`state/sent_log.json`·`briefs/{날짜}.md`를 커밋합니다.

**설정 순서**
1. GitHub 저장소 **Settings → Secrets and variables → Actions**에 위 시크릿 등록.
2. **Actions → Daily News Briefing → Run workflow**(`workflow_dispatch`)로 즉시 검증
   (`dry-run`으로 먼저 점검 권장).
3. 첫 2~3회 실행 로그를 확인하며 다듬기.

> 참고: Claude Code on the web의 예약 세션(Scheduled Session)으로 동일 cron을 걸어
> `python run.py`를 실행하도록 구성할 수도 있습니다.

## 테스트
```bash
python -m pytest tests -q     # 오프라인 결정론 단계 검증 (네트워크/LLM 불필요)
```
URL 정규화, 과병합 방지(시간/수치/버전), 사전필터, 다양성 캡, push/아카이브 분리,
번역 표기, 페이지 IR의 스키마·저신뢰도 제외·폴백 degraded 표시, 그리고 관련도
채점·분류·무관 기사 제외(한국어 부분 문자열 충돌·해외 중앙은행·대소문자 회귀 표본 포함)·
후보 풀 20건 캡·새/구형 selection finalize·조용한 날 0건 브리핑·IR 중요도 순서·`분류:` 표식을
검증합니다.

## 프로젝트 구조
```
news/
  model.py        공통 기사 모델 (계약)
  config.py       env/시크릿 + 튜너블
  http.py         공유 HTTP + 피드 fetch/검증/파싱
  state.py        sent_log + 쿼리 캐시
  feeds.py        피드 URL 레지스트리
  collectors/     source_a / source_b / source_c
  normalize.py    정규화 + 언어 감지
  prefilter.py    키워드 하드 필터
  dedup.py        URL 정규화 + 제목 셰글링 + 과병합 방지
  relevance.py    지표 중심 관련도 채점 (indicators·category·score)
  indicator_keywords.json  지표·보유 종목 키워드 + 가중치
  select.py       1회 LLM 선별 + 다양성 캡 (점수순 휴리스틱 폴백)
  summarize.py    1회 LLM 한 줄 요약 + 지표 태그 + 번역 (무추론 폴백)
  render.py       한국어 섹션 렌더 + 시장 분위기 + push/아카이브 분리
  fallback.py     결정론적 최소 경로
  pipeline.py     오케스트레이션 + 자동 폴백
run.py            엔트리포인트
.github/workflows/daily-briefing.yml   스케줄
state/sent_log.json                    발송 이력
briefs/{날짜}.md                        일일 아카이브
```
