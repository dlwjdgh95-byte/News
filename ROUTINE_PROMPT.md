# Claude 루틴(예약 세션) 설정 — 복붙용

> [!NOTE]
> **이 단독 브리핑 루틴은 Miner 레포의 통합 루틴으로 대체되었다.**
> 현재 운영 절차는 `Miner/invest-wiki/ROUTINE_PROMPT.md`의 "세션 절차" — 하나의 루틴이
> 뉴스 브리핑(A단계, 이 레포의 파이프라인 그대로) → invest-wiki ingest(B단계) →
> 종합 브리핑 발송(C단계)을 순서대로 수행한다. 아래 내용은 이 레포의 파이프라인을
> 단독 루틴으로 돌릴 때의 참고/백업용으로 유지한다.

매일 07:30 KST 브리핑을 **Claude 루틴**으로 돌리는 설정. 신뢰성 단계(수집·중복제거·관련도
채점·전달·폴백)는 파이썬이 결정론적으로 처리하고, **선별 + 한 줄 요약 1패스만 구독 모델(루틴
세션)**이 수행한다. 토큰 목표: LLM 입력 ≈ 후보 20건 × (제목 + 스니펫 120자), 출력 ≈ 한 줄 요약
8개(지표 중심 설계, 2026-11-04 — 자세한 계약은 `ORCHESTRATION.md`).

## 사전 체크리스트
1. **코드가 기본 브랜치에 있어야 함.** 루틴은 매 실행마다 레포의 *기본 브랜치*를 클론한다.
   현재 코드는 `claude/daily-news-briefing-pehmpy`에 있으므로, 이 브랜치를 `main`에 머지하거나
   기본 브랜치로 지정해야 한다. (안 하면 아래 프롬프트 1단계에서 `git checkout`으로 브랜치를
   먼저 받도록 해야 함.)
2. **환경변수(=시크릿) 등록** — claude.ai/code 에서 환경 설정 열기(클라우드 아이콘 → 환경 선택 →
   설정 아이콘). `.env` 형식, 한 줄에 `KEY=value`, **따옴표 금지**:
   ```
   TELEGRAM_BOT_TOKEN=123456:abc...
   TELEGRAM_CHAT_ID=숫자형_chat_id
   NEWSDATA_API_KEY=선택
   GUARDIAN_API_KEY=선택
   ```
   ⚠️ 하이브리드에서는 선별·요약을 **구독 모델**이 하므로 `ANTHROPIC_API_KEY` 불필요.
   ⚠️ 전용 시크릿 저장소가 없어 환경을 편집할 수 있는 사람에게 값이 보인다(개인용은 무방).
3. **네트워크 허용(필수).** 같은 환경 설정 대화상자에서 **Network access → Custom**, Allowed domains에
   아래를 추가(또는 간단히 **Full**). 안 하면 모든 외부 요청이 `403 host_not_allowed`로 막힌다.
   ```
   news.google.com
   newsdata.io
   content.guardianapis.com
   api.telegram.org
   www.coindesk.com
   cointelegraph.com
   decrypt.co
   ```
   "Also include default list of common package managers" 체크(파이썬 설치용).

## 루틴 만들기
- claude.ai/code/routines → **New routine** (또는 CLI에서 `/schedule`).
- **Repositories:** 이 레포 선택.
- **Model:** 구독 모델(안정적인 상위 모델) 선택.
- **Environment:** 위에서 시크릿·네트워크를 설정한 환경 선택.
- **Trigger → Schedule:** 시간은 **로컬(한국) 시간**으로 입력하면 자동 UTC 변환됨 → **매일 07:30** 설정.
  (폼 프리셋이 daily만 있으면 daily 선택 후 CLI `/schedule update`로 미세조정. 최소 간격 1시간.)
- 저장 후 **Run now**로 즉시 1회 검증.

## 루틴 프롬프트 (그대로 붙여넣기)

```
당신은 매일 아침 한국어 경제·시사 뉴스 브리핑의 리드 오케스트레이터다.
레포에는 결정론적 파이프라인이 이미 구현돼 있다. 당신의 역할은 "선별 1회 + 요약 1회"뿐이며,
수집·중복제거·전달·폴백은 파이썬이 처리한다. 아래를 순서대로 정확히 수행하라.

0) (코드가 기본 브랜치에 없다면) `git checkout claude/daily-news-briefing-pehmpy` 후 진행.
   `pip install -r requirements.txt` 실행.

1) `python run.py --prepare` 실행하고 출력 JSON을 확인하라.
   - "mode"가 "prepare-failed"이면(수집 실패) → `python run.py --fallback` 실행 후 종료.
   - "quiet": true이면(지표·보유 종목 관련 기사 0건 — 조용한 날) → 2·3단계를 건너뛰고 바로
     4단계 `python run.py --finalize`를 실행하라. 0건 정상 브리핑이 기록된다(폴백 금지).
   - 그 외에는 반드시 진행하라(빈 폴백으로 새지 말 것). `state/candidates.json`만 읽어라
     (`state/pool.json`은 머신 전용 — 읽지 말 것; 다른 레포 파일 탐색도 불필요).
     최상위: max_items(기본 8), diversity_caps, indicator_keys(쓸 수 있는 지표 키).
     후보(관련도 점수순, 최대 20건): id, title, source + 있을 때만 snippet(≤120자),
     age_h(발행 후 경과 시간), category(central_bank/data/policy/geopolitics/holding),
     indicators(결정론 지표 태그), related_count.

2) 후보 풀 전체를 한 번에 검토해 **핵심 지표나 보유 종목을 움직이는 정치·경제 뉴스**만
   최대 max_items(기본 8)건 골라 각각 한 줄로 요약하라.
   - 다양성 캡: 한 매체(source)당 최대 2건(후보에는 한 매체가 3건까지 있을 수 있다).
   - 근거 강제: 제공된 title + snippet만 근거로 하라. 스니펫 범위를 넘는 추론 금지.
   - one_liner: 무엇이 일어났는지 한 문장. 해설·전망은 쓰지 마라.
   - 번역: 한국어가 아닌 기사는 title에 한국어 번역 제목을 넣어라(파이썬이 "번역 (원문: 원제)"로 병기).
   - indicators: 이 뉴스가 움직일 지표 키를 indicator_keys 안에서만 골라라. 후보의 태그를 그대로
     써도 되고 고쳐도 된다. 빈 목록 []은 "지표 없음"(결정론 태그를 지움), 생략하면 파이썬이
     결정론 태그를 쓴다.
   - 미근거 단정(likely/clearly/will definitely 등)은 flags에 "unsourced-claim" 추가.
   - 신뢰도: 근거가 약하면 confidence를 낮춰라(0.45 미만은 페이지 제외, 아카이브).
   - events: *앞으로 예정된* 일정(지표·FOMC/금통위·실적·만기·표결 등)을 최대 5개 한 줄로. 없으면 [].
   - why_it_matters·implications·top_insight·whats_changed·themes·market_mood는 쓰지 않는다
     (선택 필드 — 있으면 렌더되지만 토큰 목표상 생략이 기본).

3) 결과를 `state/selection.json`에 아래 형식으로 저장하라(id는 candidates의 id, 중요도 순 —
   페이지 IR의 items가 이 순서 그대로 나가고 다운스트림이 앞 N건을 쓴다):
   {"selected":[{"id":0,"title":"한국어 제목","one_liner":"한 줄 요약","indicators":["ust10y"],
     "confidence":0.8,"flags":[]}],
    "events":["美 10월 CPI 발표(11/13 22:30 KST)"]}

4) `python run.py --finalize` 실행. 출력의 "delivered"가 0/N이면(전송 실패) `python run.py --fallback`
   실행. selection.json이 없거나 비면 finalize가 자동으로 휴리스틱 선별로 대체한다.

5) 상태 영속화: `git add state/sent_log.json briefs/ && git commit -m "chore: briefing $(date -u +%F)"`
   후 기본 브랜치로 푸시하라(루틴 권한에서 "Allow unrestricted branch pushes"를 켜야 main 푸시 가능).
   재발송 방지(sent_log)와 아카이브가 다음 실행에 반영되려면 이 단계가 필요하다.

성공/실패와 발송 건수를 마지막에 한 줄로 보고하라.
```

## 백업 스케줄러
`.github/workflows/daily-briefing.yml`(cron `30 22 * * *` UTC = 07:30 KST)은 자율 일체형 경로의
백업이다. 루틴과 동시에 켜도 `sent_log` 덕분에 중복 발송은 안 되지만, 혼선을 피하려면 하나만 활성화
권장(루틴을 주로 쓰면 Actions는 비활성화).
```
