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
    dangling = {"rules": [{"name": "금리", "category": "central_bank", "indicators": [],
                           "keywords": ["금리 인하"], "unless": ["@없는 규칙"]}],
                "weights": {}, "category_priority": list(relevance.CATEGORIES)}
    with pytest.raises(ValueError):
        relevance.Config(dangling)


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


# --- 한국어 부분 문자열 충돌 (리뷰 지적, 실제 지난 기사 포함) ----------------------
def _rules(title, summary=""):
    """requires_any(지정학 조건)와 무관하게, 키워드가 맞은 규칙 이름 집합."""
    cfg = relevance.load()
    fields = (("title", cfg.mask(title)), ("body", cfg.mask(summary)))
    return {r.name for r in cfg.rules if r.place(fields)}


@pytest.mark.parametrize("title,rule", [
    ("트럼프, 40분 유엔 연설서 북한은 한 번도 언급하지 않았다", "한국은행"),
    ("트럼프 대통령 방한은 이번이 두 번째", "한국은행"),
    ("美 금리 인하 기대…시한은 12월", "한국은행"),
    ("대출 상한은 그대로", "한국은행"),
    ("수입 제한은 없다", "한국은행"),
    ("감독 권한은 금융위로", "한국은행"),
    ("연내 타결될 것이란 전망", "지정학"),
    ("하락한 이유가 뭘까", "유가"),
    ("표현의 자유가 위축됐다", "유가"),
    ("자금 여유가 생겼다", "유가"),
    ("외국인 보유가 늘었다", "유가"),
    ("국제 금융시장 불안 확대", "금"),
    ("국제 금리 하락세", "금"),
    ("지금 가격이 싸다", "금"),
    ("지금 시세로는 손해", "금"),
    ("추경호 원내대표 기자회견", "재정"),
    ("공급망 다원화 전략", "원달러"),
    ("창구 일원화 추진", "원달러"),
    ("행사 참가자 모집", "지정학"),
    ("코스피 종전 최고치 경신", "지정학"),
    ("거래소, 워시트레이딩 적발", "연준"),
])
def test_korean_substring_collisions_do_not_hit(title, rule):
    assert rule not in _rules(title), (title, _rules(title))


def test_north_korea_un_speech_is_not_bok_news():
    # 실제 지난 기사: '북한은'의 '한은' 때문에 central_bank/bok_rate 5.0점이었다.
    a = _scored("트럼프, 40분 유엔 연설서 북한은 한 번도 언급하지 않았다")
    assert a.rel_category == "" and a.indicators == []


def test_deadline_word_does_not_strip_fed_tags():
    # '시한은'의 '한은'이 일반 금리 규칙의 unless에 걸려 연준 태그가 빠지던 버그.
    a = _scored("美 금리 인하 기대…시한은 12월")
    assert a.rel_category == "central_bank"
    assert {"fed_upper", "fomc_cut"} <= set(a.indicators) and "bok_rate" not in a.indicators


@pytest.mark.parametrize("title,rule", [
    ("한은, 기준금리 동결", "한국은행"),
    ("[한은] 금리 결정 앞두고", "한국은행"),
    ("\"한은 연내 추가 인상\"", "한국은행"),
    ("국제유가 급등", "유가"),
    ("고유가 장기화", "유가"),
    ("저유가 시대 끝나나", "유가"),
    ("유가 급락에 정유주 약세", "유가"),
    ("이란 공습", "지정학"),
    ("美, '이란 핵시설' 타격", "지정학"),
    ("대이란 제재 강화", "지정학"),
    ("친이란 무장세력 공격", "지정학"),
    ("금값 사상 최고", "금"),
    ("국제 금값 4천달러", "금"),
    ("금 현물 4,143달러 시험", "금"),
    ("金값 급등", "금"),
    ("금 가격 고공행진", "금"),
    ("정부, 추경 편성 착수", "재정"),
    ("원화 약세 지속", "원달러"),
    ("원화값 급락", "원달러"),
    ("이스라엘, 가자지구 공습", "지정학"),
    ("가자 휴전 협상 재개", "지정학"),
    ("우크라 종전 협상 급물살", "지정학"),
    ("이란 \"美에 종전 조건 전달\"", "지정학"),
    ("워시 연준 의장 발언", "연준"),
])
def test_korean_intended_matches_still_hit(title, rule):
    assert rule in _rules(title), (title, _rules(title))


# --- 해외 중앙은행·일반 영문 낱말 ----------------------------------------------
@pytest.mark.parametrize("title", ["영란은행 금리 인하", "호주중앙은행 금리 인상",
                                   "RBI cuts interest rates", "ECB holds interest rates",
                                   "Bank of England rate cut bets rise"])
def test_foreign_central_banks_do_not_get_fed_tags(title):
    a = _scored(title)
    assert a.rel_category == "central_bank", (title, a.rel_category)
    assert a.indicators == ["dxy"], (title, a.indicators)


def test_fed_still_tagged_when_foreign_bank_also_named():
    a = _scored("Fed and ECB both cut interest rates")
    assert {"fed_upper", "fomc_cut", "dxy"} <= set(a.indicators)


@pytest.mark.parametrize("title,absent", [
    ("India increases growth forecast for fiscal year", "ust10y"),
    ("Company marks 10-year anniversary", "ust10y"),
    ("Stocks hit two-year high", "ust2y"),
    ("Colombia trade deficit narrows", "ust10y"),
    ("Palm oil prices surge on export curbs", "wti"),
    ("Korea wins gold medal in archery", "gold"),
    ("日 외환개입 경계감", "usdkrw"),
    ("Corporate Treasuries Slow Bitcoin Buying", "ust10y"),   # 크립토 '트레저리' ≠ 미 국채
    ("Bitmine Expands ETH Treasury", "ust10y"),
    ("Startup lands billion-dollar valuation", "dxy"),
    ("시중은행 대출금리 인상", "fed_upper"),
])
def test_generic_english_words_narrowed(title, absent):
    assert absent not in _scored(title).indicators, (title, _scored(title).indicators)


@pytest.mark.parametrize("title,present", [
    ("Congress weighs fiscal stimulus", "ust10y"),
    ("Budget deficit widens to record", "ust10y"),
    ("10-year Treasury yield tops 4.6%", "ust10y"),
    ("Two-year yields jump after CPI", "ust2y"),
    ("Crude oil slips as OPEC meets", "wti"),
    ("Gold hits record as dollar slides", "gold"),
    ("외환당국 개입에 원/달러 환율 급락", "usdkrw"),
])
def test_intended_english_and_fx_matches_still_hit(title, present):
    assert present in _scored(title).indicators, (title, _scored(title).indicators)


# --- 대소문자 -------------------------------------------------------------------
@pytest.mark.parametrize("title,category,key", [
    ("U.S. Dollar Slides After Jobs Data", "data", "dxy"),
    ("Korean Won Weakens Past 1,450", "data", "usdkrw"),
    ("Gold ETF Inflows Jump", "data", "gold"),
    ("NASDAQ Hits Record High", "data", "vix"),
    ("Senate Advances Clarity Act", "policy", None),
    ("BITMINE ADDS MORE ETH", "holding", None),
])
def test_title_case_and_all_caps_headlines_match(title, category, key):
    a = _scored(title)
    assert a.rel_category == category, (title, a.rel_category)
    if key:
        assert key in a.indicators


@pytest.mark.parametrize("title", ["Travel Tips For A Winter Getaway",   # TIPS ≠ Tips
                                   "Back in the hood for the holidays",  # HOOD ≠ hood
                                   "Lifting weights for 30 sec intervals",  # SEC ≠ sec
                                   "Ethics committee opens probe"])      # ETH ≠ Ethics
def test_case_sensitive_acronyms_stay_strict(title):
    assert _scored(title).rel_category == "", title


def test_bok_mixed_case_acronym():
    a = _scored("BoK raises interest rates")
    assert a.rel_category == "central_bank" and a.indicators == ["bok_rate"]   # 연준 키 없음(unless)


@pytest.mark.parametrize("title,absent", [
    ("Inflation data jolts markets", "claims"),   # JOLTS ≠ jolts
    ("Bok choy prices soar", "bok_rate"),         # BOK ≠ Bok
    ("Ohtani adds 2 rbi in win", "dxy"),          # RBI ≠ rbi (야구 타점)
])
def test_case_sensitive_acronyms_added_in_review(title, absent):
    assert absent not in _scored(title).indicators, (title, _scored(title).indicators)


# --- 규칙별 mask·unless ----------------------------------------------------------
def test_customs_office_mention_does_not_disable_tariff_rule():
    # 예전 unless는 기사 전체를 봤다 — '관세청'만 스쳐도 관세 규칙이 꺼졌다.
    assert "관세·무역" in _rules("관세청 집계…美 관세 부과 뒤 대미 수출 급감")
    assert "관세·무역" not in _rules("관세청, 10월 수출입 동향 발표")   # 관세청 ⊃ 관세는 지운다


def test_unless_is_per_field():
    # 스니펫에 한은이 나와도 제목의 '美 금리 인하'는 연준 지표를 단다.
    a = _scored("美 금리 인하 기대 확산", summary="한은도 연내 인하를 검토 중이다.")
    assert {"fed_upper", "fomc_cut", "bok_rate"} <= set(a.indicators)
    # 제목에 한은이 있으면 제목의 '금리 동결'은 연준으로 새지 않는다.
    assert _scored("한은, 금리 동결").indicators == ["bok_rate"]


@pytest.mark.parametrize("title,summary,expected", [
    ("이창용 '금리 인하 신중'", "시장에서는 내년 초 금리 인하를 점치고 있다.", ["bok_rate"]),
    ("RBA hikes to 18-year high", "Interest rates rose by 25 basis points.", ["dxy"]),
    ("영란은행, 금리 인하", "금리 인하 폭은 0.25%p다.", ["dxy"]),
])
def test_unless_in_title_also_blocks_snippet(title, summary, expected):
    # 제목이 한은·해외 중앙은행 기사면 스니펫의 일반 금리 낱말도 그 은행 얘기다 — 연준 키 금지.
    a = _scored(title, summary=summary)
    assert a.rel_category == "central_bank"
    assert a.indicators == expected, (title, a.indicators)


# --- 고용·달러·기타 좁히기 (2차 리뷰) ------------------------------------------
@pytest.mark.parametrize("title", ["재고용 확대", "삼성전자 미국 공장 고용 1500명",
                                   "영국 고용시장 약세 지속", "UK jobs market stays soft",
                                   "DOSM: Labour market stability supports Malaysia's outlook"])
def test_claims_not_tagged_for_hiring_or_non_us_labour_news(title):
    assert "claims" not in _scored(title).indicators, (title, _scored(title).indicators)


@pytest.mark.parametrize("title", ["미 8월 고용 16만2천 명 증가", "부진한 美 고용지표에 국채금리 하락",
                                   "비트코인, 8월 고용 서프라이즈에 8만 달러 붕괴",
                                   "Fed watches cooling labor market", "美 신규 고용 15만 명"])
def test_claims_tagged_for_us_labour_data(title):
    assert "claims" in _scored(title).indicators, (title, _scored(title).indicators)


def test_claims_context_can_come_from_snippet():
    a = _scored("노동시장 냉각 신호", summary="미국 7월 구인 건수가 줄었다.")
    assert "claims" in a.indicators


@pytest.mark.parametrize("title", ["美 고용 부진", "미국 고용 둔화", "미 고용 둔화", "美 고용 호조에 달러 강세"])
def test_us_employment_trend_phrases_tag_claims(title):
    assert "claims" in _scored(title).indicators, (title, _scored(title).indicators)


def test_claims_context_must_share_the_field_with_the_match():
    # 제목의 '국내 고용시장'은 스니펫의 '미국 관세'로 미국 노동시장 기사가 되지 않는다.
    a = _scored("국내 고용시장 한파", summary="미국 관세 영향으로 제조업 채용이 줄었다.")
    assert "claims" not in a.indicators
    # 제목의 맥락은 스니펫에도 통한다.
    assert "claims" in _scored("美 경제 지표 점검", summary="노동시장 냉각이 이어졌다.").indicators
    # 'Latin American'은 미국 맥락이 아니다.
    assert "claims" not in _scored("Latin American labor market tightens").indicators


def test_mask_keeps_leading_boundary_unless_marked():
    # 'ether treasury' 같은 마스크가 'Tether Treasury' 안에서 'Treasury'를 지우면 안 된다.
    assert "ust10y" in _scored("Tether Treasury bills holdings top $130 billion").indicators
    # '*billion-dollar'처럼 표시한 마스크만 앞 경계 없이 지운다.
    assert "dxy" not in _scored("Firm signs multibillion-dollar deal").indicators
    assert "dxy" not in _scored("Firm signs multimillion dollar deal").indicators


@pytest.mark.parametrize("title", ["Australian dollar slides", "Taiwan dollar hits record",
                                   "Startup raises cash in multibillion-dollar deal",
                                   "Dollar-backed stablecoins grow", "Hong Kong dollar peg tested"])
def test_foreign_dollars_and_dollar_adjectives_do_not_tag_dxy(title):
    assert "dxy" not in _scored(title).indicators, (title, _scored(title).indicators)


def test_us_dollar_still_tags_dxy():
    assert "dxy" in _scored("US dollar rises after jobs data").indicators


def test_more_korean_boundaries_from_review():
    assert "wti" not in _scored("카드사 회원유치 경쟁 과열").indicators
    assert "wti" in _scored("국제 원유 공급 감소").indicators
    assert _scored("현금 ETF로 자금 몰려").rel_category == ""
    assert _scored("금 ETF 자금 유입 사상 최대").rel_category in ("holding", "data")
    assert _scored("대선배 기업인의 조언").rel_category == ""


def test_ticker_notation_masked_but_plain_nasdaq_matches():
    assert _scored("Micron Technology (NASDAQ:MU) Stock Gets Price Target").rel_category == ""
    assert _scored("Vanguard ETF (NASDAQ: VGLT) hits 52-week low").rel_category == ""
    a = _scored("Stocks rise on the Nasdaq: Apple up 2%")
    assert a.rel_category == "data" and "vix" in a.indicators


# --- 선거 규칙 축소 -------------------------------------------------------------
@pytest.mark.parametrize("title", ["특검, 선거법 항소심 선고", "지방선거 공천 갈등 격화",
                                   "There's an election at the school board"])
def test_minor_elections_are_not_policy_news(title):
    assert _scored(title).rel_category == "", title


@pytest.mark.parametrize("title", ["美 중간선거 D-10, 상원 초접전", "대선 후보 TV 토론",
                                   "Japan calls snap election", "US midterms: what markets expect"])
def test_national_elections_are_policy_news(title):
    assert _scored(title).rel_category == "policy", title


# --- 놓치던 기사 회귀 표본 (리뷰어 예시) -----------------------------------------
@pytest.mark.parametrize("title,category,must_have", [
    ("Dollar inches up as markets weigh Iran sanctions, Treasury buybacks",
     "geopolitics", {"dxy", "ust10y"}),
    ("美 7월 구인 727만 건…노동시장 냉각", "data", {"claims"}),
    ("채권시장이 힘을 과시하는 이유", "data", {"ust10y"}),
    ("트럼프 '클래리티법' 압박…", "policy", set()),
    ("美 연방부채 40조 달러", "policy", {"ust10y"}),
    ("JOLTS job openings fall to 7.2 million", "data", {"claims"}),
    ("美 국채 5% 돌파 '비상'", "data", {"ust10y"}),
])
def test_previously_missed_articles_are_kept(title, category, must_have):
    a = _scored(title)
    assert a.rel_category == category, (title, a.rel_category, a.indicators)
    assert must_have <= set(a.indicators), (title, a.indicators)


@pytest.mark.parametrize("title", ["Bitcoin tops $90K as ETF inflows return",
                                   "비트코인, 8만 달러 회복", "BTC 반등에 알트코인 동반 상승"])
def test_bitcoin_counts_as_crypto_holding_proxy(title):
    # 🪙 섹션이 늘 비던 원인 — 지난 크립토 기사 291건 중 165건이 비트코인만 언급해 빠졌다.
    assert _scored(title).rel_category == "holding", title


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
    # 명시적 빈 목록 = "움직일 지표 없음" → 결정론 태그를 지운다.
    assert relevance.clean_indicators([]) == []
    # 유효 키가 하나도 없으면(오타뿐) 결정론 태그 유지.
    assert relevance.clean_indicators(["bogus"]) is None


def test_llm_can_clear_or_keep_deterministic_tags():
    from news import summarize

    def applied(item):
        a = _scored("원·달러 환율 1,450원 돌파")
        assert a.indicators == ["usdkrw"]
        summarize.apply_item(a, {"title": a.title, "one_liner": "요약", "confidence": 0.8,
                                 "flags": [], **item})
        return a.indicators

    assert applied({"indicators": []}) == []              # 명시적 빈 목록 → 지운다
    assert applied({}) == ["usdkrw"]                      # 키 없음 → 유지
    assert applied({"indicators": None}) == ["usdkrw"]    # null → 유지
    assert applied({"indicators": ["bogus"]}) == ["usdkrw"]  # 유효 키 0 → 유지
    assert applied({"indicators": ["ust10y"]}) == ["ust10y"]


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


# --- 조용한 날 = 정상 0건 브리핑 (폴백 아님) ---------------------------------------
_NOISE = ["가을 단풍 명소 추천", "주말 맛집 탐방 성수동", "신작 게임 출시 소식", "반려견 산책 요령"]


def _no_fallback(monkeypatch):
    def boom(reason, persist=True):
        raise AssertionError(f"quiet day must not fall back: {reason}")
    monkeypatch.setattr(pipeline, "run_fallback_path", boom)


def _assert_quiet_outputs(ir, md):
    assert ir["quiet"] is True and ir["items"] == []
    assert "degraded" not in ir
    assert render.QUIET_LINE in md
    assert "관련 기사 없음" in md and "발송 기준(신뢰도) 미달" not in md
    for t in _NOISE:
        assert t not in md, "filtered-out headlines must not resurface"


def test_quiet_day_hybrid_prepare_then_finalize(hybrid_paths, monkeypatch):
    noise = [_a(t, source_name=f"생활{i}") for i, t in enumerate(_NOISE)]
    monkeypatch.setattr(pipeline, "_collect_all", lambda: (noise, []))
    _no_fallback(monkeypatch)

    out = pipeline.run_prepare()
    assert out["mode"] == "prepare" and out["quiet"] is True and out["candidates"] == 0
    assert out["irrelevant_dropped"] == len(_NOISE)
    view = json.loads(pipeline.CANDIDATES_PATH.read_text(encoding="utf-8"))
    assert view["quiet"] is True and view["candidates"] == []

    # 에이전트가 빈(혹은 엉뚱한) selection을 써도 조용한 날 브리핑이 나온다.
    _write_selection({"selected": [], "events": []})
    result = pipeline.run_finalize_safe(persist=True)
    assert result["mode"] == "finalize" and result["quiet"] is True
    _assert_quiet_outputs(*_finalize_outputs(result))


def test_quiet_day_autonomous_run(hybrid_paths, monkeypatch):
    noise = [_a(t, source_name=f"생활{i}") for i, t in enumerate(_NOISE)]
    monkeypatch.setattr(pipeline, "_collect_all", lambda: (noise, []))
    _no_fallback(monkeypatch)
    result = pipeline.run(persist=True)
    assert result["mode"] == "main" and result["quiet"] is True
    assert result["selected"] == 0
    _assert_quiet_outputs(*_finalize_outputs(result))


def test_empty_collection_still_falls_back(hybrid_paths, monkeypatch):
    monkeypatch.setattr(pipeline, "_collect_all", lambda: ([], ["A", "B", "C"]))
    called = {}
    monkeypatch.setattr(pipeline, "run_fallback_path",
                        lambda reason, persist=True: called.setdefault("reason", reason))
    pipeline.run(persist=True)
    assert "no articles" in called["reason"]


def test_candidates_view_drops_per_cluster_cap(hybrid_paths, monkeypatch):
    monkeypatch.setattr(pipeline, "_collect_all",
                        lambda: ([_a("환율 1,450원 돌파", source_tag=SOURCE_B)], []))
    pipeline.run_prepare()
    view = json.loads(pipeline.CANDIDATES_PATH.read_text(encoding="utf-8"))
    assert view["diversity_caps"] == {"per_source": config.MAX_PER_SOURCE}
    assert "quiet" not in view


# --- IR 순서 = selected 순서 -----------------------------------------------------
def test_ir_items_follow_agent_rank_not_source_tag(hybrid_paths):
    _write_pool(_sample_pool())   # 0:B 1:B 2:A 3:C
    _write_selection({"selected": [
        {"id": 3, "title": "비트마인, 이더리움 추가 매입", "one_liner": "추가 매입", "confidence": 0.9, "flags": []},
        {"id": 2, "title": "이란 긴장에 유가 급등", "one_liner": "유가 급등", "confidence": 0.9, "flags": []},
        {"id": 0, "title": "美 10년물 금리 4.6% 돌파", "one_liner": "금리 돌파", "confidence": 0.9, "flags": []},
    ]})
    ir, _md = _finalize_outputs(pipeline.run_finalize(persist=True))
    assert [i["one_liner"] for i in ir["items"]] == ["추가 매입", "유가 급등", "금리 돌파"]


def test_ir_items_follow_score_rank_on_heuristic_path():
    arts = [_a(f"환율 소식 {i}", source_name=f"매체{i}", source_tag=tag, confidence=0.9)
            for i, tag in enumerate([SOURCE_C, SOURCE_A, SOURCE_B, SOURCE_A])]
    for i, a in enumerate(arts):
        a.score = float(i)
        a.one_liner = a.title
        a.evidence = "근거"
    chosen, _ = select.select(arts, max_items=8)
    ir = render.render_ir(chosen)
    assert [i["title"] for i in ir["items"]] == ["환율 소식 3", "환율 소식 2", "환율 소식 1", "환율 소식 0"]


# --- md 분류 표식 · 빈 섹션 문구 ---------------------------------------------------
def test_md_carries_category_marker_after_indicator_line(hybrid_paths):
    _write_pool(_sample_pool())
    _write_selection({"selected": [
        {"id": 0, "title": "美 10년물 금리 4.6% 돌파", "one_liner": "금리 돌파", "confidence": 0.9, "flags": []},
        {"id": 3, "title": "비트마인, 이더리움 추가 매입", "one_liner": "추가 매입", "confidence": 0.9, "flags": []},
    ]})
    ir, md = _finalize_outputs(pipeline.run_finalize(persist=True))
    lines = md.splitlines()
    i = lines.index("지표: ust10y")            # 지표 줄 형식은 그대로
    assert lines[i + 1] == "분류: data"
    assert "분류: holding" in lines             # 지표가 없어도 분류 줄은 단다
    assert [it["category"] for it in ir["items"]] == ["data", "holding"]
    import re
    marker = re.compile(r"(?:분류|카테고리|category):\s*([A-Za-z_가-힣]+)")   # Miner 파서
    assert [m.group(1) for m in marker.finditer(md)] == ["data", "holding"]


def test_empty_section_text_distinguishes_nothing_relevant_from_archived():
    econ = _a("환율 1,450원 돌파", source_tag=SOURCE_B, confidence=0.9)
    econ.one_liner, econ.evidence = "환율 상승", "근거"
    _tg, md, _ = render.render_briefing([econ])
    world = md.split("**🌐 시사·국제**")[1].split("**🪙")[0]
    assert "· 관련 기사 없음" in world and "발송 기준" not in world

    weak = _a("이란 공습에 유가 급등", source_tag=SOURCE_A, confidence=0.2)
    weak.one_liner = "유가 급등"
    _tg, md, _ = render.render_briefing([econ, weak])
    world = md.split("**🌐 시사·국제**")[1].split("**🪙")[0]
    assert "발송 기준(신뢰도) 미달 — 아카이브 참고" in world


# --- 휴리스틱 외국어 기사 · 요약 토큰 ------------------------------------------------
def test_heuristic_non_korean_article_does_not_repeat_title():
    from news import summarize
    a = _a("Oil jumps as Iran tensions flare", language="en", confidence=0.7,
           source_tag=SOURCE_A)
    summarize._heuristic_one(a)
    assert a.title == "Oil jumps as Iran tensions flare (원문 미번역)"
    assert a.one_liner == ""
    ko = _a("국제유가 급등", language="ko")
    summarize._heuristic_one(ko)
    assert ko.one_liner == "국제유가 급등"
    _tg, md, _ = render.render_briefing([a])
    block = md.split("**🌐 시사·국제**")[1].split("**🪙")[0]   # 기사 블록(헤드라인 3줄 제외)
    assert block.count("Oil jumps as Iran tensions flare") == 1


def test_summary_max_tokens_scales_with_items():
    from news import summarize
    assert summarize._max_tokens(8) == 2000
    assert summarize._max_tokens(14) > summarize._max_tokens(8)
    assert summarize._max_tokens(100) == 4000


# --- 관련도 단계 예외 통과 · 모의 LLM 요약 -------------------------------------------
def test_relevance_exception_passes_articles_through_unharmed(hybrid_paths, monkeypatch):
    # 채점 버그가 하루를 죽이면 안 된다 — 예외면 채점 없이 그대로 다음 단계로.
    arts = [_a("가을 단풍 명소 추천", source_name="생활"),
            _a("환율 1,450원 돌파", source_name="연합뉴스", source_tag=SOURCE_B)]
    monkeypatch.setattr(pipeline, "_collect_all", lambda: (arts, []))

    def boom(*a, **k):
        raise RuntimeError("scoring bug")
    monkeypatch.setattr(relevance, "filter_relevant", boom)

    out = pipeline.run_prepare()
    assert out["mode"] == "prepare" and out["candidates"] == 2
    assert out["irrelevant_dropped"] == 0 and "quiet" not in out
    pool = json.loads(pipeline.POOL_PATH.read_text(encoding="utf-8"))["candidates"]
    assert {c["title"] for c in pool} == {"가을 단풍 명소 추천", "환율 1,450원 돌파"}
    assert all(c["url"].startswith("https://n.com/") for c in pool)


def test_fallback_gather_survives_scoring_exception(monkeypatch):
    from news import fallback, http
    entries = [SimpleNamespace(title="연준 금리 동결", link="https://f.com/9", source_name="연합뉴스",
                               summary="", published=NOW)]
    monkeypatch.setattr(http, "fetch_feed", lambda url, name: entries)
    monkeypatch.setattr(relevance, "score_all", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    items = fallback._gather([("feed", "https://feed")], SOURCE_A, 10)
    assert [a.title for a in items] == ["연준 금리 동결"]


def test_llm_summarize_with_new_response_shape(monkeypatch):
    from news import llm, summarize
    arts = [_scored("美 국채 10년물 금리 4.6% 돌파", language="ko"),
            _scored("Oil jumps as Iran tensions flare", language="en"),
            _scored("원·달러 환율 1,450원 돌파", language="ko")]
    calls = {}

    def fake_complete(system, user, *, max_tokens):
        calls["max_tokens"] = max_tokens
        calls["user"] = user
        return {"items": [
            {"id": 0, "title": "美 10년물 금리 4.6% 돌파", "one_liner": "10년물 금리가 4.6%를 넘었다.",
             "indicators": ["ust10y", "not_a_key"], "confidence": 0.82, "flags": []},
            {"id": 1, "title": "이란 긴장에 유가 급등", "one_liner": "유가가 뛰었다.",
             "indicators": [], "confidence": 0.7, "flags": ["unsourced-claim"]},
            # id 2 누락 → 휴리스틱(제목 = 한 줄 요약, 결정론 태그 유지)
        ]}
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", fake_complete)

    assert summarize.summarize(arts) == "llm"
    assert calls["max_tokens"] == summarize._max_tokens(3)
    assert "indicators" in calls["user"] and "ust10y" in calls["user"]
    a0, a1, a2 = arts
    assert a0.one_liner == "10년물 금리가 4.6%를 넘었다." and a0.indicators == ["ust10y"]
    assert a0.confidence == 0.82
    assert a1.title == "이란 긴장에 유가 급등 (원문: Oil jumps as Iran tensions flare)"
    assert a1.indicators == [] and "unsourced-claim" in a1.flags   # 명시적 [] → 지움
    assert a2.one_liner == a2.title and a2.indicators == ["usdkrw"]
    assert a2.evidence.endswith("(LLM 미사용)")
