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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed")
