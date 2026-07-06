# Claude 루틴(예약 세션) 설정 — 복붙용

매일 07:30 KST 브리핑을 **Claude 루틴**으로 돌리는 설정. 신뢰성 단계(수집·중복제거·전달·폴백)는
파이썬이 결정론적으로 처리하고, **선별 1회 + 요약 1회만 구독 모델(루틴 세션)**이 수행한다.

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
   - "mode"가 "prepare-failed"이면(수집 자체 실패) → `python run.py --fallback` 실행 후 종료.
   - 그 외에는 반드시 진행하라(빈 폴백으로 새지 말 것). `state/candidates.json`만 읽어라
     (`state/pool.json`은 머신 전용 — 읽지 말 것; 다른 레포 파일 탐색도 불필요). 후보 필드:
     id, tag(A/B/C), source, title, confidence + 있을 때만 original_title, lang, category,
     sentiment, snippet, age_h(발행 후 경과 시간), cluster_id, flags, related_count.
   - candidates.json에는 `yesterday_digest`(어제 브리핑 링크 제거 다이제스트)도 들어 있다 —
     연속성 인사이트에 활용하라.

2) 후보 풀 전체를 한 번에 검토해 최종 기사를 최대 max_items(기본 14)건 선별하고 각각 요약하라.
   - 다양성 캡: 한 매체(source)당 최대 2건, 한 클러스터(cluster_id)당 최대 2건.
   - 균형: 경제·시사/국제·크립토를 고루. 연예·스포츠는 이미 제외됨.
   - 근거 강제: 제공된 title + snippet만 인용하라. 스니펫 범위를 넘는 추론 금지.
   - 번역: lang이 ko가 아니면 title에 한국어 번역 제목을 넣어라(파이썬이 "번역 (원문: 원제)"로
     병기). original_title은 보존됨.
   - 미근거 단정(likely/clearly/will definitely 등)은 flags에 "unsourced-claim" 추가. 기존 flags 유지.
   - 신뢰도: 근거가 약하면 confidence를 낮춰라(낮은 confidence는 push 제외, 아카이브).
   - implications(2차 영향·함의): 각 기사에 "누구에게 어떤 영향인지 / 무엇을 지켜봐야 하는지"를 한 줄로.
   - events: *앞으로 예정된* 일정(지표·FOMC/금통위·실적·만기·표결 등)을 최대 5개 한 줄로. 없으면 [].

   그리고 **개별 요약을 넘어 맥락을 잇는 인사이트**를 아래 브리핑 레벨 필드로 생성하라:
   - top_insight(오늘의 관전 포인트, 2~3줄): 개별 기사 요약이 아니라 여러 기사를 엮은 종합 해석.
     예: "금리 동결 + 유가 급등 + 환율 1,540원 → 물가 재점화 우려가 증시 상단을 누르는 구도."
   - whats_changed(어제 대비, 최대 4줄): `yesterday_digest`와 비교해 전개·수치 변화만.
     예: "비트코인: 어제 6.2만 → 오늘 6.0만, 스트래티지 매각 발표가 트리거." (어제 자료 없으면 [])
   - themes(오늘의 핵심 테마, 최대 3개): 기사들을 관통하는 테마를 "테마 — 한 줄"로 묶어라.

3) 결과를 `state/selection.json`에 아래 형식으로 저장하라(id는 candidates의 id, 중요도 순):
   {"selected":[{"id":0,"title":"한국어 제목","one_liner":"한줄요약",
     "why_it_matters":"왜 중요한지","implications":"2차 영향·함의","tags":["태그"],
     "confidence":0.82,"evidence":"인용 구절 + 출처","flags":[]}],
    "top_insight":["관전 포인트 1","관전 포인트 2"],
    "whats_changed":["어제 대비 변화 1"],
    "themes":["테마1 — 한 줄","테마2 — 한 줄","테마3 — 한 줄"],
    "events":["美 5월 CPI 발표(21:30 KST)","삼성전자 2분기 잠정실적 발표"],
    "market_mood":"시장 분위기 한 줄(선택)"}

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
