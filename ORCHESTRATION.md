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
   → 수집(A·B·C 병렬) → 정규화 → 사전필터 → 다단계 중복제거 → 발송이력 대조
   → 풀 캡(MAX_CANDIDATES, 태그 균형) 적용
   → state/candidates.json  (에이전트용 슬림 뷰: URL 없음, 스니펫 절단)
   → state/pool.json        (머신용 전체 상태: --finalize가 사용)
        │
[리드 에이전트 / 구독 모델]  candidates.json 1패스 선별+요약
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

**토큰 절약 설계:** 에이전트가 읽는 candidates.json에는 선별·요약에 필요한 필드만 남긴다.
수백 자짜리 Google News URL(`url`/`canonical_url`/`related`)과 `key_entities`, 빈 필드는
제외되고, 선택은 `id`로만 한다. URL 복원·발송은 pool.json을 읽는 `--finalize`가 처리한다.
어제 브리핑도 전문 대신 링크 제거 다이제스트(`yesterday_digest`)로 제공된다.
어느 단계든 실패/타임아웃/빈 결과면 `run.py --fallback`(결정론적 폴백)으로 최소 브리핑 보장.
폴백 IR은 `degraded: true`로 표시돼 페이지가 "조용한 날"이 아니라 "고장난 날"로 렌더한다.

## 리드 에이전트 절차 (예약 세션 프롬프트에 넣을 내용)

1. **준비:** `python run.py --prepare` 실행.
   - 결과 JSON의 `"mode"`가 `"prepare-failed"`이거나 후보가 0이면 → `python run.py --fallback`
     실행 후 종료.
   - 성공이면 `state/candidates.json`(만)을 읽는다. 각 후보: `id, tag(A/B/C), source, title,
     confidence` + 있을 때만 `original_title(원문이 title과 다를 때), lang, category, sentiment,
     snippet, age_h(발행 후 경과 시간), cluster_id, flags, related_count`.
     (`state/pool.json`은 머신 전용 — 읽지 말 것.)

2. **선별 + 요약 (단일 추론 패스, 구독 모델):**
   후보 풀 전체를 한 번에 검토하여 최종 기사(최대 `max_items`, 기본 14)를 선별하고 각 기사를 요약한다.
   - **다양성 캡:** 한 매체당 최대 `diversity_caps.per_source`(2), 한 클러스터(`cluster_id`)당 최대
     `diversity_caps.per_cluster`(2). (파이썬 `--finalize`가 안전망으로 재강제하지만, 에이전트가 1차로 지킬 것.)
   - **근거 강제:** 제공된 `title` + `snippet`만 인용. 스니펫 범위를 넘는 추론 금지.
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

4. **확정 아카이브:** `python run.py --finalize` 실행. `briefs/<date>.md`·`briefs/<date>.json`
   기록과 sent_log 갱신까지 수행.
   - `selection.json`이 없거나 비면 `--finalize`가 자동으로 휴리스틱 선별+요약으로 대체.
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
