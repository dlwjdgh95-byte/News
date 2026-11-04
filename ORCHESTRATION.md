# 오케스트레이션 — 하이브리드 (리드 에이전트 + 구독 모델)

> [!NOTE]
> 이 브리핑 파이프라인은 현재 **Miner 레포의 통합 루틴**(`Miner/invest-wiki/ROUTINE_PROMPT.md`)의
> A단계로 실행된다 — 브리핑 발송 직후 invest-wiki가 같은 세션에서 브리핑을 ingest하고
> 종합 브리핑을 이어서 발송한다. 아래는 파이프라인 자체의 절차 정의로 계속 유효하다.

이 문서는 매일 07:30 KST 브리핑을 **Claude 예약 세션(Scheduled Session)**으로 돌릴 때의
리드 에이전트 절차를 정의한다. 신뢰성이 필요한 부분(수집·정규화·중복제거·전달·폴백)은 전부
파이썬이 결정론적으로 처리하고, **단 하나의 "지능" 단계(선별 1회 + 요약 1회)만** 리드 에이전트가
구독 모델로 수행한다. (스펙의 "구독된 안정적 모델" + "리드 오케스트레이터" 의도를 충족.)

## 데이터 흐름
```
[Python] run.py --prepare
   → 수집(A·B·C 병렬) → 정규화 → 사전필터 → 다단계 중복제거
   → 관련도 채점(news/relevance.py: indicators·category·score, 무관 기사 제외)
   → 발송이력 대조 → 풀 캡(MAX_CANDIDATES=20, 점수순 + 매체당 3·클러스터당 2 캡)
   → state/candidates.json  (에이전트용 슬림 뷰: URL 없음, 스니펫 120자, 지표 태그)
   → state/pool.json        (머신용 전체 상태: --finalize가 사용)
        │
[리드 에이전트 / 구독 모델]  candidates.json 1패스 선별 + 한 줄 요약 (최대 8건)
   → state/selection.json  작성
        │
[Python] run.py --finalize
   → 다양성 캡 재적용 → 구조화 렌더 → 아카이브 2종 → sent_log 기록
        │
        ├─ briefs/<date>.md    (invest-wiki collect_news.py가 읽는 ingest 소스)
        └─ briefs/<date>.json  (리포트 페이지 IR — 루틴이 Miner로 복사)
```

**전달 경로 (2026-08-15 변경):** 텔레그램 발송은 없어졌다. 브리핑은 Miner 레포의 정적
리포트 페이지 첫 탭(`뉴스`)으로 나간다. 루틴이 `briefs/<date>.json`을
`Miner/briefing/data/news/<date>.json`으로 복사·커밋하면 `render_html.py`가 렌더한다.
장애 알림만 텔레그램에 남아 있고, 그 코드는 이 레포가 아니라
`Miner/invest-wiki/scripts/notify.py`에 있다.

**토큰 절약 설계 (지표 중심, 2026-11-04):** 무관한 기사는 LLM 앞에서 결정론으로 걸러내고
(`news/relevance.py` — 지표·보유 종목 키워드, 필수 분류 central_bank/data/policy/
geopolitics/holding), 남은 기사를 점수순 20건으로 자른다. candidates.json의 후보 행은
`id, title, source` + 있을 때만 `snippet(≤120자), age_h, category, indicators, related_count`뿐이다.
URL·`key_entities`·confidence·flags 등은 빠지고, 선택은 `id`로만 한다. URL 복원은 pool.json을
읽는 `--finalize`가 처리한다. **토큰 목표: LLM 입력 ≈ 20건 × (제목 + 120자), 출력 ≈ 한 줄 요약
8개.** 어제 브리핑 다이제스트(`yesterday_digest`)는 기본 끔 — `NEWS_YESTERDAY_DIGEST_CHARS`로
켜면 그때만 실린다.
어느 단계든 실패/타임아웃/수집 0건이면 `run.py --fallback`(결정론적 폴백)으로 최소 브리핑 보장.
폴백 IR은 `degraded: true`로 표시돼 페이지가 "조용한 날"이 아니라 "고장난 날"로 렌더한다.
**조용한 날은 고장이 아니다:** 수집은 됐는데 관련도 필터를 통과한 기사가 0건이면 `--prepare`가
실패하지 않고 `quiet: true` + 후보 0건을 낸다. 이때 `--finalize`가 0건 정상 브리핑(md에 "오늘은
지표와 관련된 필수 뉴스가 없습니다", IR에 `quiet: true`·빈 `items`, `degraded` 없음)을 쓴다.
걸러낸 무관한 헤드라인을 폴백으로 되살리지 않는다.

## 리드 에이전트 절차 (예약 세션 프롬프트에 넣을 내용)

1. **준비:** `python run.py --prepare` 실행.
   - 결과 JSON의 `"mode"`가 `"prepare-failed"`이면(수집 자체 실패) → `python run.py --fallback`
     실행 후 종료.
   - 결과 JSON에 `"quiet": true`가 있으면(후보 0건 — 지표·보유 종목 관련 기사가 하나도 없는 날)
     → 선별·요약을 건너뛰고 바로 4단계 `python run.py --finalize`를 실행한다. 0건 정상 브리핑이
     기록된다(폴백이 아니다 — `--fallback`을 돌리지 말 것).
   - 그 외에는 `state/candidates.json`(만)을 읽는다. 최상위: `max_items`(기본 8),
     `diversity_caps`(`per_source`), `indicator_keys`(쓸 수 있는 지표 키 27개), `failed_sources`
     (+ 켰을 때만 `yesterday_digest`). 각 후보(점수순, 최대 20건): `id, title, source` + 있을 때만
     `snippet(≤120자), age_h(발행 후 경과 시간), category, indicators, related_count`.
     (`state/pool.json`은 머신 전용 — 읽지 말 것.)

2. **선별 + 한 줄 요약 (단일 추론 패스, 구독 모델):**
   후보 풀 전체를 한 번에 검토해 **핵심 지표나 보유 종목을 움직이는 정치·경제 뉴스**만 최대
   `max_items`(기본 8)건 고르고, 각 기사에 한 줄 요약과 지표 태그를 단다.
   - **기준:** `category`(central_bank·data·policy·geopolitics·holding)와 `indicators`를 참고해
     지표 영향이 큰 순.
   - **다양성 캡:** 한 매체당 최대 `diversity_caps.per_source`(2). 후보 풀에는 한 매체가 3건까지
     있을 수 있으니 그중 2건만 고른다. (`--finalize`가 안전망으로 재강제.)
   - **근거 강제:** 제공된 `title` + `snippet`만 근거로. 스니펫 범위를 넘는 추론 금지.
   - **한 줄 요약(`one_liner`):** 무엇이 일어났는지 한 문장. 해설·전망은 쓰지 않는다.
   - **번역:** 한국어가 아닌 기사는 `title`에 한국어 번역 제목(파이썬이 `번역 (원문: Original)`로 병기).
   - **지표(`indicators`):** 이 뉴스가 움직일 지표 키(`indicator_keys` 안에서만). 후보의 결정론
     태그를 그대로 쓰거나 고친다. 빈 목록 `[]`은 "움직일 지표 없음"이라 결정론 태그를 지운다.
     생략(또는 null)하면 파이썬이 결정론 태그를 쓴다. 모르는 키는 버려진다(유효 키가 하나도
     없으면 결정론 태그 유지).
   - **플래그·신뢰도:** 출처에 귀속되지 않는 단정(`likely/clearly/will definitely` 류)은 `flags`에
     `unsourced-claim`. 근거가 약하면 `confidence`를 낮게(0.45 미만은 페이지에서 빠지고 아카이브로).
   - **events:** *앞으로 예정된* 일정(지표 발표·FOMC/금통위·실적·만기·표결 등) 최대 5개. 없으면 [].

3. **selection.json 작성:** `state/selection.json`에 아래 형식으로 저장(`id`는 candidates의 id,
   중요도 순).
   ```json
   {"selected":[{"id":0,"title":"한국어 제목","one_liner":"한 줄 요약","indicators":["ust10y"],"confidence":0.8,"flags":[]}],
    "events":["美 10월 CPI 발표(11/13 22:30 KST)"]}
   ```
   - `selected`가 `max_items`를 넘으면 앞쪽 `max_items`건만 쓴다(그래서 중요도 순으로 적는다).
     IR `items`도 이 순서 그대로 나간다 — 다운스트림(Miner 시장 브리핑·블로그)이 앞 N건을 가장
     중요한 기사로 쓴다. md는 소스별 섹션 배치를 유지하고, 기사마다 `지표:` 줄 아래 `분류:` 줄을
     단다.
   - **선택(구형) 필드:** `why_it_matters`·`implications`·`tags`·`evidence`(기사별),
     `top_insight`·`whats_changed`·`themes`·`market_mood`(브리핑 레벨)는 쓰지 않아도 된다. 있으면
     그대로 렌더하고, 없으면 해당 줄·섹션을 생략한다 — 예전 형식의 selection.json도 그대로 받는다.

4. **확정 아카이브:** `python run.py --finalize` 실행. `briefs/<date>.md`·`briefs/<date>.json`
   기록과 sent_log 갱신까지 수행.
   - `selection.json`이 없거나 비면 `--finalize`가 자동으로 휴리스틱으로 대체한다(관련도 점수
     상위 8건, 한국어 기사는 제목을 그대로 한 줄 요약으로 — 번역 못 한 외국어 기사는 한 줄 요약을
     비운다, 지표 태그는 결정론 값).
   - 조용한 날(`--prepare`가 `quiet: true`)에는 `selection.json` 내용과 상관없이 0건 정상
     브리핑을 쓴다.
   - 발송 단계가 없어졌으므로 이중 발송 방지 장치도 함께 사라졌다. 폴백은 수집·렌더가
     실제로 실패했을 때만 돈다.

## 시크릿
예약 세션 환경에 필요 시 `NEWSDATA_API_KEY`, `GUARDIAN_API_KEY`를 주입한다. 텔레그램
시크릿은 이 레포에서 더 이상 쓰지 않는다(발송 제거). 하이브리드에서는 선별·요약을
**구독 모델(세션 내 추론)**이 하므로 `ANTHROPIC_API_KEY`도 불필요하다.

## 백업 스케줄러
`/.github/workflows/daily-briefing.yml`은 자율 일체형 경로(`python run.py`)를 실행하는
**결정론적 백업**이다. 예약 세션이 누락되어도 `briefs/<date>.{md,json}`이 남는다.
둘을 동시에 켤 경우 `sent_log` 덕분에 같은 기사를 양쪽에서 중복 선별하지 않는다.

**단, 이 백업 경로는 Miner로 push하지 않는다.** 루틴이 누락돼 이 워크플로만 도는 날은
리포트 페이지의 뉴스 탭이 비어 있다(설계상 의도 — News가 Miner 쓰기 권한을 갖지 않게
하기 위한 선택). 그날 뉴스를 페이지에 올리려면 루틴을 수동 재실행한다.

## 스펙 매핑
- "읽기전용 서브에이전트 A·B·C": 결정론·정시 보장을 위해 런타임 수집은 파이썬 collector 모듈
  (`news/collectors/source_{a,b,c}.py`)이 스레드로 병렬 수행한다. 리드 에이전트는 직접 수집하지 않고
  취합 결과(candidates.json)에 대해 선별·요약·전달만 담당한다 — 스펙의 "리드는 직접 수집하지 않는다"와 일치.
