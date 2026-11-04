"""지표 중심 관련도 채점 + 슬림 LLM 계약 테스트 (네트워크·LLM 없음).

Run: python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from itertools import count
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news.model import Article, SOURCE_A, SOURCE_B, SOURCE_C
from news import config, pipeline, relevance, render, select

NOW = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
_ids = count()

# Miner briefing/indicators.yaml의 표시 지표 키 — 계약이라 테스트에 그대로 박는다.
SPEC_KEYS = [
    "fed_upper", "ust2y", "ust10y", "real10y", "bok_rate", "fomc_hike", "fomc_cut",
    "bei10y", "core_yoy", "core_3m", "wti", "brent", "gold", "copper", "copper_gold",
    "curve_10y2y", "curve_10y3m", "claims", "kr_exports", "vix", "hy_oas", "net_liq",
    "m2_yoy", "dxy", "usdkrw", "kospi", "kr_foreign",
]


def _a(title, **kw):
    kw.setdefault("source_name", "Test")
    kw.setdefault("source_tag", SOURCE_A)
    kw.setdefault("url", f"https://n.com/{next(_ids)}")
    return Article(title=title, **kw)


def _scored(title, **kw):
    return relevance.score_article(_a(title, **kw), now=NOW)


@pytest.fixture
def hybrid_paths(tmp_path, monkeypatch):
    """prepare/finalize가 쓰는 모든 경로를 임시 폴더로 돌리고 LLM을 끈다."""
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    monkeypatch.setattr(config, "BRIEFS_DIR", briefs)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "SENT_LOG_PATH", tmp_path / "sent_log.json")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(pipeline, "CANDIDATES_PATH", tmp_path / "candidates.json")
    monkeypatch.setattr(pipeline, "POOL_PATH", tmp_path / "pool.json")
    monkeypatch.setattr(pipeline, "SELECTION_PATH", tmp_path / "selection.json")
    return tmp_path


def _write_pool(arts):
    pipeline.POOL_PATH.write_text(json.dumps(
        {"failed_sources": [], "candidates": [{"id": i, **a.to_dict()} for i, a in enumerate(arts)]},
        ensure_ascii=False), encoding="utf-8")


def _write_selection(data):
    pipeline.SELECTION_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _finalize_outputs(result):
    ir = json.loads(open(result["ir_path"], encoding="utf-8").read())
    md = open(result["brief_path"], encoding="utf-8").read()
    return ir, md


# --- 키워드 설정 ------------------------------------------------------------
def test_keyword_config_covers_exactly_the_indicator_keys():
    assert list(relevance.INDICATOR_KEYS) == SPEC_KEYS
    assert relevance.load().covered_indicators() == set(SPEC_KEYS)


def test_unknown_indicator_key_in_config_is_rejected():
    bad = {"rules": [{"name": "오타", "category": "data", "indicators": ["fed_uper"],
                      "keywords": ["연준"]}],
           "weights": {}, "category_priority": list(relevance.CATEGORIES)}
    with pytest.raises(ValueError):
        relevance.Config(bad)


# --- 분류·지표 부착 ----------------------------------------------------------
@pytest.mark.parametrize("title,category,must_have", [
    ("연준 파월, 12월 금리 인하 시사", "central_bank", {"fed_upper", "fomc_hike", "fomc_cut"}),
    ("FOMC는 기준금리를 동결했다", "central_bank", {"fed_upper"}),  # 한글 조사가 붙어도 매칭
    ("Fed Holds Rates Steady Amid Inflation Concerns", "central_bank", {"fed_upper", "core_yoy"}),
    ("美 10월 CPI 3.1% 상승…근원 물가 둔화", "data", {"core_yoy", "core_3m"}),
    ("원·달러 환율 1,450원 돌파", "data", {"usdkrw"}),
    ("이란 공습에 국제유가 급등", "geopolitics", {"wti", "brent"}),
    ("트럼프, 반도체 관세 25% 예고", "policy", {"core_yoy", "kr_exports", "usdkrw"}),
    ("비트마인, 이더리움 추가 매입 공시", "holding", set()),
    ("Robinhood shares jump on crypto volume", "holding", set()),
    ("Kospi slides as chipmakers drag", "data", {"kospi"}),
    ("[3분증시] 뉴욕증시, 기술주 급락", "data", {"vix"}),
    ("워시 연준 의장, 매파 발언", "central_bank", {"fed_upper"}),
])
def test_category_and_indicators(title, category, must_have):
    a = _scored(title)
    assert a.rel_category == category, (title, a.rel_category, a.indicators)
    assert must_have <= set(a.indicators), (title, a.indicators)
    assert a.indicators == sorted(a.indicators, key=relevance.INDICATOR_KEYS.index)
    assert a.score > 0


@pytest.mark.parametrize("title", ["한은, 금리 동결…이창용 '인하 신중'",
                                   "신현송 총재 \"금리 인하 서두르지 않겠다\""])
def test_bok_news_does_not_get_fed_tags(title):
    # 일반 금리 낱말("금리 동결")은 한은 기사에서 연준 지표로 새지 않는다(unless).
    a = _scored(title)
    assert a.rel_category == "central_bank"
    assert a.indicators == ["bok_rate"]


def test_geopolitics_without_oil_fx_rates_is_dropped():
    a = _scored("러시아·우크라이나 3자 회담 10일 재개")
    assert a.rel_category == ""
    kept, dropped = relevance.filter_relevant([a], now=NOW)
    assert kept == [] and dropped == 1


def test_irrelevant_lifestyle_entertainment_sports_dropped():
    noise = ["가을 단풍 명소 베스트 5", "아이돌 그룹 컴백 무대 화제", "손흥민 시즌 3호골",
             "주말 맛집 탐방 — 성수동 카페", "GTX-A 삼성역 무정차 통과 연기",
             "유가족, 진상 규명 촉구 기자회견",   # '유가'는 mask로 걸러진다
             "금융위기 이후 최대 미술품 경매",     # '금융위'(금융위원회) ⊄ '금융위기'
             "Turmoil at the film festival",     # 'oil' ⊄ 'turmoil'
             "Ethics panel opens hearing",       # 'eth' ⊄ 'ethics'
             "Travel tips for a winter getaway"]  # TIPS(대문자)는 tips와 다르다
    arts = [_a(t) for t in noise] + [_a("환율 1,400원 돌파")]
    kept, dropped = relevance.filter_relevant(arts, now=NOW)
    assert [a.title for a in kept] == ["환율 1,400원 돌파"]
    assert dropped == len(noise)


# --- 점수 구성 ---------------------------------------------------------------
def test_score_components():
    base = _scored("연준 금리 동결").score
    assert _scored("연준 금리 동결", source_name="Reuters").score == base + 1.0   # 통신사
    assert _scored("연준 금리 동결", source_name="reuters").score == base + 1.0   # 소문자 source_id
    two = _a("연준 금리 동결", related=["https://x.com/1", "https://x.com/2"])
    one = _a("연준 금리 동결", related=["https://x.com/1"])
    assert relevance.score_article(two, now=NOW).score == base + 1.0              # 복수 매체
    assert relevance.score_article(one, now=NOW).score == base
    assert _scored("연준 금리 동결에 이더리움 반등").score == base + 2.0          # 보유 종목
    assert _scored("연준 금리 동결", published_at=NOW - timedelta(hours=12)).score == base - 0.5
    assert _scored("연준 금리 동결", published_at=NOW - timedelta(hours=72)).score == base - 2.0
    # 제목 적중(2) > 스니펫만 적중(1)
    assert _scored("오늘의 시장", summary="원·달러 환율이 올랐다").score == 1.0
    assert _scored("원·달러 환율 상승").score == 2.0


def test_clean_indicators():
    assert relevance.clean_indicators(["usdkrw", "ust10y", "bogus"]) == ["ust10y", "usdkrw"]
    assert relevance.clean_indicators("wti") == ["wti"]
    assert relevance.clean_indicators(None) is None
    assert relevance.clean_indicators([]) is None
    assert relevance.clean_indicators(["bogus"]) is None


# --- 후보 풀 / 에이전트 뷰 ----------------------------------------------------
def test_default_caps():
    if "NEWS_MAX_CANDIDATES" not in os.environ:
        assert config.MAX_CANDIDATES == 20
    if "NEWS_MAX_ITEMS" not in os.environ:
        assert config.MAX_ITEMS == 8 == pipeline.MAX_ITEMS
    if "NEWS_SNIPPET_CHARS" not in os.environ:
        assert config.SNIPPET_CHARS == 120


def test_candidates_view_is_slim():
    a = _scored("美 국채 10년물 금리 4.6% 돌파", summary="가" * 500, language="ko",
                category="business", sentiment="negative", source_tag=SOURCE_B,
                published_at=NOW, related=["https://x.com/1"], flags=["conflicting-figures"])
    d = a.to_llm_dict(0, snippet_chars=120)
    assert set(d) <= {"id", "title", "snippet", "source", "age_h", "category",
                      "indicators", "related_count"}
    assert len(d["snippet"]) == 120
    assert d["category"] == "data"                    # 관련도 분류지, 수집 카테고리(business)가 아니다
    assert d["indicators"] == ["ust10y"]
    assert d["related_count"] == 1


def test_prepare_drops_irrelevant_and_caps_pool_at_20(hybrid_paths, monkeypatch):
    monkeypatch.setattr(config, "MAX_CANDIDATES", 20)
    monkeypatch.setattr(config, "YESTERDAY_DIGEST_CHARS", 0)
    kws = ["환율", "유가", "연준", "물가", "코스피", "관세"]
    relevant = [_a(f"{kws[i % len(kws)]} 항목{i}", source_name=f"매체{i}", source_tag=SOURCE_B)
                for i in range(30)]
    noise = [_a(t, source_name=f"생활{i}") for i, t in enumerate(
        ["가을 단풍 명소 추천", "주말 맛집 탐방 성수동", "신작 게임 출시 소식",
         "반려견 산책 요령", "겨울철 피부 관리법"])]
    monkeypatch.setattr(pipeline, "_collect_all", lambda: (relevant + noise, []))

    out = pipeline.run_prepare()
    assert out["candidates"] == 20 and out["irrelevant_dropped"] == 5

    view = json.loads(pipeline.CANDIDATES_PATH.read_text(encoding="utf-8"))
    assert view["max_items"] == 8
    assert view["indicator_keys"] == SPEC_KEYS
    assert "yesterday_digest" not in view, "digest is opt-in (default 0)"
    cands = view["candidates"]
    assert len(cands) == 20
    assert all(c["category"] and "생활" not in c["source"] for c in cands)
    allowed = {"id", "title", "snippet", "source", "age_h", "category", "indicators",
               "related_count"}
    assert all(set(c) <= allowed for c in cands)
    # 점수순: pool.json의 score가 내림차순이다.
    pool = json.loads(pipeline.POOL_PATH.read_text(encoding="utf-8"))["candidates"]
    scores = [c.get("score", 0.0) for c in pool]
    assert scores == sorted(scores, reverse=True)


# --- finalize: 새 최소 계약 / 구형 계약 / 휴리스틱 ------------------------------
def _sample_pool():
    arts = [
        _a("美 국채 10년물 금리 4.6% 돌파", source_name="Reuters", source_tag=SOURCE_B,
           confidence=0.6),
        _a("원·달러 환율 1,450원 돌파", source_name="연합뉴스", source_tag=SOURCE_B),
        _a("Oil jumps as Iran tensions flare", source_name="Bloomberg", language="en",
           source_tag=SOURCE_A),
        _a("비트마인, 이더리움 추가 매입", source_name="CoinDesk", source_tag=SOURCE_C),
    ]
    relevance.score_all(arts, now=NOW)
    return arts


def test_finalize_with_new_minimal_selection(hybrid_paths):
    _write_pool(_sample_pool())
    _write_selection({
        "selected": [
            {"id": 0, "title": "美 10년물 금리 4.6% 돌파", "one_liner": "미 국채 10년물 금리가 4.6%를 넘었다.",
             "indicators": ["ust10y", "not_a_key"], "confidence": 0.8, "flags": []},
            # indicators 누락 → 결정론 태그(wti, brent)로 대체
            {"id": 2, "title": "이란 긴장에 유가 급등", "one_liner": "이란 긴장 고조로 유가가 뛰었다.",
             "confidence": 0.7, "flags": []},
        ],
        "events": ["美 10월 CPI 발표(11/13 22:30 KST)"],
    })
    result = pipeline.run_finalize(persist=True)
    assert result["selection"] == "agent"
    ir, md = _finalize_outputs(result)

    assert ir["schema"] == render.IR_SCHEMA
    by_title = {i["title"]: i for i in ir["items"]}
    rates = by_title["美 10년물 금리 4.6% 돌파"]
    assert rates["indicators"] == ["ust10y"] and rates["category"] == "data"
    assert rates["one_liner"] == "미 국채 10년물 금리가 4.6%를 넘었다."
    assert rates["why_it_matters"] == ""            # 기존 필드 이름은 유지, 값만 빈다
    assert "implications" not in rates and "tags" not in rates
    oil = by_title["이란 긴장에 유가 급등 (원문: Oil jumps as Iran tensions flare)"]
    assert oil["indicators"] == ["wti", "brent"] and oil["category"] == "geopolitics"
    assert ir["events"] == ["美 10월 CPI 발표(11/13 22:30 KST)"]
    assert ir["top_insight"] == [] and ir["whats_changed"] == [] and ir["themes"] == []

    assert "지표: ust10y" in md and "지표: wti, brent" in md
    for heading in ("📌 **오늘의 헤드라인**", "**💹 경제·시장**", "**🌐 시사·국제**",
                    "**🪙 크립토 (보조)**", "🗓 **오늘 주목할 이벤트**"):
        assert heading in md, heading
    assert "관전 포인트" not in md and "💡" not in md


def test_finalize_with_old_full_selection(hybrid_paths):
    _write_pool(_sample_pool())
    _write_selection({
        "selected": [
            {"id": 1, "title": "원·달러 환율 1,450원 돌파", "one_liner": "환율이 1,450원을 넘었다.",
             "why_it_matters": "수입물가 압력", "implications": "수입 기업 원가 부담 주시",
             "tags": ["환율"], "confidence": 0.82, "evidence": "원문 인용 — 연합뉴스", "flags": []},
        ],
        "top_insight": ["환율 상단 돌파 → 물가 재점화 우려"],
        "whats_changed": ["어제 1,430원 → 오늘 1,450원"],
        "themes": ["환율 — 상단 돌파"],
        "events": ["금통위(11/27)"],
        "market_mood": "위험 회피",
    })
    result = pipeline.run_finalize(persist=True)
    assert result["selection"] == "agent"
    ir, md = _finalize_outputs(result)

    item = ir["items"][0]
    assert item["why_it_matters"] == "수입물가 압력"
    assert item["implications"] == "수입 기업 원가 부담 주시" and item["tags"] == ["환율"]
    assert item["indicators"] == ["usdkrw"] and item["category"] == "data"  # 결정론 대체
    assert ir["top_insight"] == ["환율 상단 돌파 → 물가 재점화 우려"]
    assert ir["whats_changed"] == ["어제 1,430원 → 오늘 1,450원"]
    assert ir["themes"] == ["환율 — 상단 돌파"]
    assert "오늘의 관전 포인트" in md and "▸ 함의: 수입 기업 원가 부담 주시" in md
    assert "지표: usdkrw" in md


def test_heuristic_selection_picks_top8_by_score(hybrid_paths):
    kws = ["환율", "유가", "연준", "물가", "코스피", "관세", "수출", "금값", "국채금리", "실업수당"]
    arts = [_a(f"{kw} 소식 {i}호", source_name=f"매체{i}", source_tag=SOURCE_B,
               confidence=0.7 if i % 2 else 0.9)
            for i, kw in enumerate(kws)]
    relevance.score_all(arts, now=NOW)
    for i, a in enumerate(arts):
        a.score = float(i)  # 신뢰도와 엇갈리게 — 점수가 이겨야 한다
    chosen, method = select.select(list(arts), max_items=8)
    assert method == "heuristic"
    assert [a.score for a in chosen] == [9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0]

    _write_pool(arts)  # selection.json 없음 → finalize가 휴리스틱으로 대체
    result = pipeline.run_finalize(persist=True)
    assert result["selection"] == "autonomous:heuristic"
    ir, md = _finalize_outputs(result)
    titles = {i["title"] for i in ir["items"]}
    assert len(ir["items"]) == 8
    assert "환율 소식 0호" not in titles and "유가 소식 1호" not in titles
    assert all(i["one_liner"] == i["title"] for i in ir["items"]), "title doubles as one_liner"
    assert all(i["indicators"] and i["category"] for i in ir["items"])


# --- 폴백 ---------------------------------------------------------------------
def test_fallback_ir_carries_indicators_and_category():
    a = _scored("국제유가 급락", confidence=0.5)
    ir = render.fallback_ir([a], reason="collection failed")
    assert ir["degraded"] is True
    assert ir["items"][0]["indicators"] == ["wti", "brent"]
    assert ir["items"][0]["category"] == "data"


def test_fallback_gather_orders_relevant_first_without_dropping(monkeypatch):
    from news import fallback, http
    old = NOW - timedelta(hours=30)
    entries = [
        SimpleNamespace(title="가을 단풍 명소 추천", link="https://f.com/1", source_name="생활",
                        summary="", published=NOW),
        SimpleNamespace(title="연준 금리 동결", link="https://f.com/2", source_name="연합뉴스",
                        summary="", published=old),
    ]
    monkeypatch.setattr(http, "fetch_feed", lambda url, name: entries)
    items = fallback._gather([("feed", "https://feed")], SOURCE_A, 10)
    assert [a.title for a in items] == ["연준 금리 동결", "가을 단풍 명소 추천"]
    assert items[0].rel_category == "central_bank" and items[1].rel_category == ""
