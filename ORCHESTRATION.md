# 오케스트레이션 — 하이브리드 (리드 에이전트 + 구독 모델)

이 문서는 매일 07:30 KST 브리핑을 **Claude 예약 세션(Scheduled Session)**으로 돌릴 때의
리드 에이전트 절차를 정의한다. 신뢰성이 필요한 부분(수집·정규화·중복제거·전달·폴백)은 전부
파이썬이 결정론적으로 처리하고, **단 하나의 "지능" 단계(선별 1회 + 요약 1회)만** 리드 에이전트가
구독 모델로 수행한다. (스펙의 "구독된 안정적 모델" + "리드 오케스트레이터" 의도를 충족.)

## 데이터 흐름
```
[Python] run.py --prepare
   → 수집(A·B·C 병렬) → 정규화 → 사전필터 → 다단계 중복제거 → 발송이력 대조
   → state/candidates.json  작성
        │
[리드 에이전트 / 구독 모델]  candidates.json 1패스 선별+요약
   → state/selection.json  작성
        │
[Python] run.py --finalize
   → 다양성 캡 재적용 → 구조화 렌더 → 텔레그램 발송(4096 분할) → 아카이브 → sent_log 기록
```
어느 단계든 실패/타임아웃/빈 결과면 `run.py --fallback`(결정론적 폴백)으로 최소 브리핑 보장.

## 리드 에이전트 절차 (예약 세션 프롬프트에 넣을 내용)

1. **준비:** `python run.py --prepare` 실행.
   - 결과 JSON의 `"mode"`가 `"prepare-failed"`이거나 후보가 0이면 → `python run.py --fallback`
     실행 후 종료.
   - 성공이면 `state/candidates.json`을 읽는다. 각 후보: `id, title, original_title, summary(snippet),
     source_name, source_tag(A/B/C), language, category, sentiment, confidence, cluster_id,
     key_entities, flags, url, canonical_url`.

2. **선별 + 요약 (단일 추론 패스, 구독 모델):**
   후보 풀 전체를 한 번에 검토하여 최종 기사(최대 `max_items`, 기본 14)를 선별하고 각 기사를 요약한다.
   - **다양성 캡:** 한 매체당 최대 `diversity_caps.per_source`(2), 한 클러스터(`cluster_id`)당 최대
     `diversity_caps.per_cluster`(2). (파이썬 `--finalize`가 안전망으로 재강제하지만, 에이전트가 1차로 지킬 것.)
   - **근거 강제:** 제공된 `title` + `summary` 스니펫만 인용. 스니펫 범위를 넘는 추론 금지.
     본문이 없으면 제목+요약 한도 내에서만.
   - **번역:** 한국어가 아닌 기사는 `title`에 한국어 번역 제목을 넣는다(파이썬이 원문을 괄호 병기:
     `번역 (원문: Original)`). `original_title`은 보존됨.
   - **미근거 단정 플래그:** 출처에 귀속되지 않는 `likely/clearly/will definitely` 류는 `flags`에
     `unsourced-claim` 추가. 수치 충돌 등 기존 `flags`는 유지.
   - **신뢰도:** 근거가 약하면 `confidence`를 낮게(파이썬이 낮은 confidence는 아카이브로 빼고 push 제외).

3. **selection.json 작성:** `state/selection.json`에 아래 형식으로 저장.
   ```json
   {
     "selected": [
       {"id": 0, "title": "한국어 제목", "one_liner": "한줄요약",
        "why_it_matters": "왜 중요한지", "tags": ["태그"],
        "confidence": 0.82, "evidence": "인용 구절 + 출처", "flags": []}
     ],
     "market_mood": "감성 기반 시장 분위기 한 줄(선택)"
   }
   ```
   `id`는 candidates.json의 후보 id. 중요도 순으로 나열.

4. **확정 발송:** `python run.py --finalize` 실행. 텔레그램 발송·아카이브·sent_log 기록까지 수행.
   - `selection.json`이 없거나 비면 `--finalize`가 자동으로 휴리스틱 선별+요약으로 대체(여전히 발송됨).
   - 텔레그램이 **한 청크라도** 전송되면 폴백을 트리거하지 않는다(이중 발송 방지). 전송 0건일 때만
     `--finalize`가 내부적으로 폴백 경로로 전환.

## 시크릿
예약 세션 환경에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`(필수), 필요 시 `NEWSDATA_API_KEY`,
`GUARDIAN_API_KEY`를 주입한다. 하이브리드에서는 선별·요약을 **구독 모델(세션 내 추론)**이 하므로
`ANTHROPIC_API_KEY`는 불필요하다.

## 백업 스케줄러
`/.github/workflows/daily-briefing.yml`(cron `30 22 * * *`)은 자율 일체형 경로(`python run.py`)를
실행하는 **결정론적 백업**이다. 예약 세션이 누락되어도 GitHub Actions가 최소 브리핑을 보장한다.
둘을 동시에 켤 경우 `sent_log` 덕분에 같은 기사를 양쪽에서 중복 발송하지 않는다(단, 발송 시각이
겹치지 않도록 한쪽을 약간 앞/뒤로 두는 것을 권장).

## 스펙 매핑
- "읽기전용 서브에이전트 A·B·C": 결정론·정시 보장을 위해 런타임 수집은 파이썬 collector 모듈
  (`news/collectors/source_{a,b,c}.py`)이 스레드로 병렬 수행한다. 리드 에이전트는 직접 수집하지 않고
  취합 결과(candidates.json)에 대해 선별·요약·전달만 담당한다 — 스펙의 "리드는 직접 수집하지 않는다"와 일치.
