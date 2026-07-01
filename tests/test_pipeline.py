"""Offline tests for the deterministic stages (no network, no LLM).

Run: python -m pytest tests/ -q   (or: python tests/test_pipeline.py)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news.model import Article, SOURCE_A, SOURCE_B, SOURCE_C
from news import normalize, prefilter, dedup, select, render


def _a(title, url, **kw):
    kw.setdefault("source_name", "Test")
    kw.setdefault("source_tag", SOURCE_A)
    return Article(title=title, url=url, **kw)


def test_canonical_url_strips_tracking():
    u1 = dedup.canonical_url("https://www.example.com/news/story?utm_source=x&id=5&fbclid=abc#top")
    u2 = dedup.canonical_url("http://example.com/news/story/?id=5")
    assert u1 == u2, (u1, u2)
    assert "utm_source" not in u1 and "fbclid" not in u1 and "#" not in u1


def test_same_canonical_url_merges():
    a = _a("Fed holds rates", "https://x.com/a?utm_source=tw")
    b = _a("Fed holds rates steady", "https://x.com/a")
    reps = dedup.deduplicate([a, b])
    assert len(reps) == 1
    assert len(reps[0].related) == 1


def test_time_and_status_cue_keeps_followups_separate():
    t0 = datetime(2026, 6, 24, 1, 0, tzinfo=timezone.utc)
    a = _a("삼성전자 영업이익 발표", "https://news.com/1", published_at=t0)
    b = _a("삼성전자 영업이익 발표 이후 주가 급락", "https://news.com/2",
           published_at=t0 + timedelta(hours=15))
    reps = dedup.deduplicate([a, b])
    assert len(reps) == 2, "status-change follow-up >12h apart must stay separate"


def test_conflicting_numbers_kept_and_flagged():
    t0 = datetime(2026, 6, 24, 1, 0, tzinfo=timezone.utc)
    a = _a("삼성전자 영업이익 10조 기록", "https://news.com/x1", published_at=t0)
    b = _a("삼성전자 영업이익 12조 기록", "https://news.com/x2", published_at=t0)
    reps = dedup.deduplicate([a, b])
    assert len(reps) == 2
    assert any("conflicting-figures" in r.flags for r in reps)


def test_entity_version_distinction():
    a = _a("Google launches Gemini Pro model", "https://t.com/p")
    b = _a("Google launches Gemini Flash model", "https://t.com/f")
    reps = dedup.deduplicate([a, b])
    assert len(reps) == 2, "Gemini Pro != Gemini Flash"


def test_prefilter_drops_sports_keeps_lifestyle():
    arts = [
        _a("손흥민 결승골 토트넘 승리", "https://s.com/1"),
        _a("아이돌 그룹 컴백 화제", "https://s.com/2"),
        _a("부동산 시장 트렌드 변화", "https://s.com/3"),
        _a("연준 금리 동결 결정", "https://s.com/4"),
    ]
    kept, dropped = prefilter.prefilter(arts)
    titles = [k.title for k in kept]
    assert dropped == 2
    assert "부동산 시장 트렌드 변화" in titles
    assert "연준 금리 동결 결정" in titles


def test_selection_diversity_caps():
    arts = []
    for i in range(5):  # 5 from same outlet -> cap should limit to 2
        arts.append(_a(f"경제 뉴스 {i}", f"https://big.com/{i}",
                       source_name="BigOutlet", source_tag=SOURCE_B, confidence=0.9))
    for i in range(3):
        arts.append(_a(f"국제 뉴스 {i}", f"https://other.com/{i}",
                       source_name=f"Outlet{i}", confidence=0.8))
    reps = dedup.deduplicate(arts)
    chosen, method = select.select(reps, max_items=10)
    from collections import Counter
    by_src = Counter(a.source_name for a in chosen)
    assert by_src["BigOutlet"] <= 2, by_src


def test_render_push_archive_split():
    high = _a("중요 뉴스", "https://h.com/1", confidence=0.8)
    high.one_liner = "고신뢰 요약"
    high.evidence = "원문 인용"
    low = _a("불확실 뉴스", "https://l.com/2", confidence=0.3)
    low.one_liner = "저신뢰 요약"
    tg, md, stats = render.render_briefing([high, low])
    assert "중요 뉴스" in tg
    assert "불확실 뉴스" not in tg  # archived, not pushed
    assert "불확실 뉴스" in md      # but present in archive
    assert stats["pushed"] == 1 and stats["archived"] == 1


def test_translation_format():
    from news import summarize
    a = _a("Fed Holds Rates Steady", "https://en.com/1", language="en",
           original_title="Fed Holds Rates Steady")
    summarize._apply_translation(a, "美 연준, 금리 동결 결정")
    assert a.title == "美 연준, 금리 동결 결정 (원문: Fed Holds Rates Steady)"


def test_telegram_split():
    from news import telegram
    big = "\n\n".join(f"섹션 {i}: " + "가" * 1000 for i in range(10))
    chunks = telegram.split_message(big, limit=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert len(chunks) > 1


# --- New regression tests for the re-examination fixes --------------------
def test_korean_entity_split():
    # B3: same template, different subject (코스피 vs 코스닥) must stay separate.
    t0 = datetime(2026, 6, 24, 1, 0, tzinfo=timezone.utc)
    a = _a("코스피 외국인 순매수 전환", "https://k.com/1", published_at=t0)
    b = _a("코스닥 외국인 순매수 전환", "https://k.com/2", published_at=t0)
    reps = dedup.deduplicate([a, b])
    assert len(reps) == 2, "코스피 != 코스닥"


def test_korean_superset_still_merges():
    # B3 sanity: one title is a superset of the other (no unique token on the
    # shorter side) -> should still merge, dedup isn't fully disabled for Korean.
    t0 = datetime(2026, 6, 24, 1, 0, tzinfo=timezone.utc)
    a = _a("삼성전자 영업이익 사상 최대", "https://s.com/a?utm_source=x", published_at=t0)
    b = _a("삼성전자 영업이익 사상 최대 기록", "https://s.com/a", published_at=t0)
    reps = dedup.deduplicate([a, b])
    assert len(reps) == 1, "near-identical Korean titles should still merge"


def test_time_gap_no_cue_separates():
    # B2: >=12h apart with no status cue is not "close publish time" -> separate.
    t0 = datetime(2026, 6, 24, 1, 0, tzinfo=timezone.utc)
    a = _a("환율 1400원 동향 보도", "https://t.com/1", published_at=t0)
    b = _a("환율 1400원 동향 보도", "https://t.com/2", published_at=t0 + timedelta(hours=14))
    reps = dedup.deduplicate([a, b])
    assert len(reps) == 2, "14h apart, no cue: must not merge (시각 근접 아님)"


def test_related_no_self_reference():
    # B4: after a confidence swap the new representative's related[] must contain
    # the OLD rep's url and never its own.
    low = _a("Fed holds rates", "https://x.com/low?utm_source=t", confidence=0.4)
    high = _a("Fed holds rates steady", "https://x.com/high", confidence=0.9)
    reps = dedup.deduplicate([low, high])
    assert len(reps) == 1
    rep = reps[0]
    assert rep.url == "https://x.com/high"
    assert rep.url not in rep.related, "rep must not reference itself"
    assert "https://x.com/low?utm_source=t" in rep.related


def test_telegram_partial_send_no_fallback():
    # B1: if ANY chunk delivered, _deliver must NOT raise (so no second send).
    from news import pipeline, telegram
    art = _a("뉴스", "https://n.com/1", confidence=0.8)
    art.canonical_url = "https://n.com/1"
    orig = telegram.send_message
    try:
        telegram.send_message = lambda *a, **k: telegram.SendResult(ok=False, sent=1, total=2)
        result, _ = pipeline._deliver("text", "md", [art], send=True, persist=False)
        assert result.any_sent and not result.ok
    finally:
        telegram.send_message = orig


def test_telegram_nothing_sent_raises_for_fallback():
    # B1: zero chunks delivered must raise so the caller can fall back.
    from news import pipeline, telegram
    art = _a("뉴스", "https://n.com/2", confidence=0.8)
    orig = telegram.send_message
    try:
        telegram.send_message = lambda *a, **k: telegram.SendResult(ok=False, sent=0, total=2)
        raised = False
        try:
            pipeline._deliver("text", "md", [art], send=True, persist=False)
        except RuntimeError:
            raised = True
        assert raised, "no delivery must raise to trigger fallback"
    finally:
        telegram.send_message = orig


def test_dry_run_does_not_persist_sent_log():
    # C2: send=False must not mutate sent_log.
    from news import pipeline, config
    p = config.SENT_LOG_PATH
    before = p.read_text(encoding="utf-8") if p.exists() else None
    art = _a("뉴스", "https://n.com/3", confidence=0.9)
    art.canonical_url = "https://n.com/3"
    pipeline._deliver("text", "md", [art], send=False, persist=False)
    after = p.read_text(encoding="utf-8") if p.exists() else None
    assert before == after, "dry-run must not change sent_log"


def test_sentiment_none_string():
    # C3: literal "None"/null must normalise to None; valid labels pass through.
    from news.collectors import source_b
    assert source_b._norm_sentiment("None") is None
    assert source_b._norm_sentiment("") is None
    assert source_b._norm_sentiment(None) is None
    assert source_b._norm_sentiment("Positive") == "positive"
    assert source_b._norm_sentiment("negative") == "negative"


def test_prefilter_no_substring_false_positive():
    # Surfaced by a live demo: "nfl" ⊂ "inflation" must NOT drop economic news.
    arts = [
        _a("Fed Holds Rates Steady Amid Inflation Concerns", "https://r.com/fed",
           summary="The Fed kept its benchmark rate unchanged.", language="en", category="world"),
        _a("NBA finals draw record viewers", "https://s.com/nba",  # real sports -> drop
           summary="basketball", category="sports"),
    ]
    kept, dropped = prefilter.prefilter(arts)
    titles = [k.title for k in kept]
    assert "Fed Holds Rates Steady Amid Inflation Concerns" in titles, "inflation wrongly dropped"
    assert dropped == 1 and not any("NBA" in t for t in titles), "real NBA story should drop"


def test_conflict_not_masked_by_incidental_number():
    # Surfaced by a live demo: shared "2" from "2분기" must not mask 10조 vs 12조.
    t0 = datetime(2026, 6, 24, 1, 0, tzinfo=timezone.utc)
    a = _a("삼성전자 2분기 영업이익 10조 전망", "https://e.com/10", published_at=t0)
    b = _a("삼성전자 2분기 영업이익 12조 전망", "https://e.com/12", published_at=t0)
    reps = dedup.deduplicate([a, b])
    assert len(reps) == 2, "10조 vs 12조 must stay separate despite shared '2분기'"
    assert any("conflicting-figures" in r.flags for r in reps)


def test_events_from_agent_list():
    # Agent-supplied events take priority and render verbatim.
    a = _a("아무 뉴스", "https://n.com/e1", confidence=0.8)
    a.one_liner = "요약"; a.evidence = "근거"
    tg, md, stats = render.render_briefing(
        [a], events=["美 5월 CPI 발표", "FOMC 회의"])
    assert "오늘 주목할 이벤트" in tg
    assert "· 美 5월 CPI 발표" in tg and "· FOMC 회의" in tg


def test_events_deterministic_fallback():
    # No agent events -> derive from titles carrying a forward-looking cue.
    a = _a("삼성전자 2분기 실적 발표 예정", "https://n.com/e2", confidence=0.8)
    a.one_liner = "요약"; a.evidence = "근거"
    b = _a("코스피 어제 2% 상승 마감", "https://n.com/e3", confidence=0.8)
    b.one_liner = "요약"; b.evidence = "근거"
    tg, md, stats = render.render_briefing([a, b])  # events=None
    assert "· 삼성전자 2분기 실적 발표 예정" in tg
    assert "어제 2% 상승" not in tg.split("주목할 이벤트")[1]  # past news not an event


def test_events_empty_placeholder():
    a = _a("특별한 일정 없는 뉴스", "https://n.com/e4", confidence=0.8)
    a.one_liner = "요약"; a.evidence = "근거"
    tg, md, stats = render.render_briefing([a])
    assert "예정된 주요 일정이 식별되지 않았습니다" in tg


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed")
