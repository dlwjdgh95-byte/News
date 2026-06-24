# 📰 매일 아침 경제·시사 뉴스 브리핑 자동화

매일 **한국시간 오전 7시 30분**, 경제·시사 뉴스를 자동 수집·중복제거·요약하여
텔레그램(`@realneeewsbot`)으로 전달하는 시스템입니다. 세 종류의 데이터 소스를
통합하고, 어떤 경우에도 최소 브리핑이 도착하도록 **결정론적 폴백**을 갖춥니다.

## 핵심 설계 원칙

1. **폴백 우선 (Fallback-first).** LLM·분석 없이도 각 RSS에서 최신 제목+링크를 모아
   텔레그램으로 보내는 단순 경로(`news/fallback.py`)를 먼저 구축했습니다. 메인
   파이프라인이 실패·타임아웃·빈 결과면 자동으로 폴백이 실행되어 **07:30에 반드시
   브리핑이 도착**합니다. 폴백 메시지에는 `[폴백 모드]`가 표기됩니다.
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

## 처리 파이프라인 (`news/pipeline.py`)

```
수집(A·B·C 병렬, 소스 격리) → 정규화(공통 모델)
  → 사전 키워드 필터(연예·스포츠 제외, 라이프 포함)
  → 다단계 중복제거(URL 정규화 → 제목 셰글링 → 시간/개체 과병합 방지)
  → 발송 이력 대조(sent_log)
  → 일괄 선별(1회 LLM 호출 + 다양성 캡: 매체당 2, 클러스터당 2)
  → 구조화 요약(1회 LLM 호출, 근거 강제)
  → 번역·정렬 → 텔레그램 발송(4096자 분할) → 아카이브 커밋
어느 단계든 실패 시 → 폴백 경로 자동 전환
```

### 공통 기사 모델 (모듈 간 계약, `news/model.py`)
제목 · 요약 · 출처링크 · 매체명 · 발행시각 · 카테고리 · 감성점수 · 소스태그(A/B/C) ·
신뢰도 · 핵심개체 · 원문제목 + 다운스트림 보강 필드(한줄요약/왜중요/태그/근거/플래그).

### 과병합 방지 (최우선 안전 규칙, `news/dedup.py`)
별개 기사를 하나로 합치는 것을 **절대 피합니다.** 애매하면 분리가 기본값입니다.
- **시간 인식:** 같은 주제라도 발행 시각이 12시간 이상 벌어지고 상태 변화 신호어
  (첫날·이후·후속·급락·철회·사과·번져 등)가 있으면 후속 보도로 보고 분리.
- **개체·버전 구별:** 핵심 개체/버전이 다르면 분리(예: Gemini Pro ≠ Gemini Flash).
  핵심 수치가 충돌하면(영업이익 10조 ≠ 12조) 둘 다 남기고 `conflicting-figures` 플래그.
- **병합 조건:** 정규화 URL 동일 **또는** (제목 Jaccard ≥ 0.6 + 발행시각 근접 + 핵심개체 일치).

### 요약 — 근거 강제 (`news/summarize.py`)
헤드라인만으로 추론하지 않고 제목+요약 스니펫만 인용합니다. 각 기사는
제목/한줄요약/왜중요한지/태그/confidence/evidence로 구조화하며, 출처에 귀속되지 않는
단정 표현은 `unsourced-claim`으로 플래그합니다. **신뢰도가 낮거나 근거가 약한 기사는
아카이브(`briefs/*.md`)에 보관하되 텔레그램 push에서는 제외**합니다.

### 번역 표기
한국어가 아닌 기사는 번역 제목을 위에, 원문을 괄호로 병기:
`美 연준, 금리 동결 결정 (원문: Fed Holds Rates Steady)`

## 출력 형식 (한국어)
오늘의 헤드라인 3줄 → 경제·시장(핵심 뉴스 + **감성 기반 시장 분위기 한 줄**)
→ 시사·국제(중립 톤) → 크립토(보조) → 오늘 주목할 이벤트.

## 실행 방법

```bash
pip install -r requirements.txt

python run.py --check       # 설정/시크릿 점검 (발송 안 함)
python run.py --dry-run     # 브리핑 생성하되 텔레그램 미발송
python run.py --fallback    # 결정론적 폴백 강제 실행
python run.py               # 전체 파이프라인 + 발송 (실패 시 자동 폴백)
```

`run.py`는 데이터 문제로 인한 소프트 실패에도 폴백 발송을 시도한 뒤 **항상 exit 0**으로
종료해, 스케줄러가 하드 실패로 처리하지 않게 합니다.

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
python tests/test_pipeline.py     # 오프라인 결정론 단계 검증 (네트워크/LLM 불필요)
```
URL 정규화, 과병합 방지(시간/수치/버전), 사전필터, 다양성 캡, push/아카이브 분리,
번역 표기, 텔레그램 분할을 검증합니다.

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
  select.py       1회 LLM 선별 + 다양성 캡 (휴리스틱 폴백)
  summarize.py    1회 LLM 구조화 요약 + 번역 (무추론 폴백)
  render.py       한국어 섹션 렌더 + 시장 분위기 + push/아카이브 분리
  telegram.py     4096자 분할 발송
  fallback.py     결정론적 최소 경로
  pipeline.py     오케스트레이션 + 자동 폴백
run.py            엔트리포인트
.github/workflows/daily-briefing.yml   스케줄
state/sent_log.json                    발송 이력
briefs/{날짜}.md                        일일 아카이브
```
