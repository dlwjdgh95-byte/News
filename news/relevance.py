"""지표 중심 관련도 채점 — LLM 앞단의 결정론 단계.

핵심 지표(Miner briefing/indicators.yaml의 키)나 보유 종목을 움직이는 정치·경제
뉴스만 남긴다. 키워드·가중치는 ``news/indicator_keywords.json`` 한 곳에 있다.

기사마다 세 필드를 붙인다:
  · indicators   — 맞은 지표 키 목록(INDICATOR_KEYS 순서로 정렬)
  · rel_category — central_bank(중앙은행 결정·발언) / data(지표 발표·시세) /
                   policy(재정·관세·선거·규제) / geopolitics(유가·환율·금리를 건드릴
                   때만) / holding(보유 종목). 어느 것에도 안 맞으면 "" →
                   ``filter_relevant``가 버린다.
  · score        — 지표 키워드 적중 + 보유 종목 보너스 + 복수 매체 보너스
                   (related ≥ 2) + 공식·통신사 보너스 − 경과 시간 감점

키워드 매칭 규칙:
  · 한글이 섞인 키워드는 부분 문자열로 맞춘다(소문자화한 본문 기준).
  · 영문 키워드는 영숫자 경계로 맞춘다 — 'oil'이 'turmoil'에, 'eth'가 'ethics'에
    걸리지 않는다. 한글 조사가 바로 붙어도('FOMC는') 맞는다(``\\b``는 한글을 단어
    문자로 봐서 이 경우를 놓친다).
  · 영문 키워드에 대문자가 하나라도 있으면 대소문자를 구분한다(약어·티커·고유명사:
    TIPS ≠ tips, HOOD ≠ hood). 전부 소문자면 구분하지 않는다.
  · ``mask`` 낱말(유가족·유가증권 등)은 매칭 전에 지운다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .model import Article

KEYWORDS_PATH = Path(__file__).with_name("indicator_keywords.json")

# Miner briefing/indicators.yaml의 지표 키(hidden 표시용 키 제외), 순서도 그 파일을
# 따른다. indicators 목록을 이 순서로 정렬해 산출물이 매일 같은 모양이 되게 한다.
INDICATOR_KEYS: Tuple[str, ...] = (
    "fed_upper", "ust2y", "ust10y", "real10y", "bok_rate", "fomc_hike", "fomc_cut",
    "bei10y", "core_yoy", "core_3m", "wti", "brent", "gold", "copper", "copper_gold",
    "curve_10y2y", "curve_10y3m", "claims", "kr_exports", "vix", "hy_oas", "net_liq",
    "m2_yoy", "dxy", "usdkrw", "kospi", "kr_foreign",
)
CATEGORIES: Tuple[str, ...] = ("central_bank", "data", "policy", "geopolitics", "holding")
_KEY_ORDER = {k: i for i, k in enumerate(INDICATOR_KEYS)}


def _alternation(terms: Sequence[str], flags: int = 0):
    """영문 키워드 묶음 → 영숫자 경계 정규식. 긴 키워드부터 시도한다."""
    if not terms:
        return None
    body = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(rf"(?<![0-9A-Za-z])(?:{body})(?![0-9A-Za-z])", flags)


class _Matcher:
    """키워드 목록 하나를 컴파일한 매처. 규칙은 모듈 docstring 참고."""

    __slots__ = ("_cased", "_uncased", "_sub")

    def __init__(self, terms: Iterable[str]):
        terms = [t.strip() for t in terms if t and t.strip()]
        ascii_terms = [t for t in terms if t.isascii()]
        self._cased = _alternation([t for t in ascii_terms if t != t.lower()])
        self._uncased = _alternation([t for t in ascii_terms if t == t.lower()])
        self._sub = tuple(sorted({t.lower() for t in terms if not t.isascii()}))

    def search(self, raw: str, low: str) -> bool:
        """raw는 마스크만 지운 원문, low는 그 소문자판."""
        if self._cased is not None and self._cased.search(raw):
            return True
        if self._uncased is not None and self._uncased.search(low):
            return True
        return any(t in low for t in self._sub)


class _Rule:
    __slots__ = ("name", "category", "indicators", "match", "unless", "requires_any")

    def __init__(self, spec: dict):
        self.name = str(spec.get("name", ""))
        self.category = spec["category"]
        self.indicators = tuple(spec.get("indicators") or ())
        self.match = _Matcher(spec["keywords"])
        self.unless = _Matcher(spec["unless"]) if spec.get("unless") else None
        self.requires_any = frozenset(spec.get("requires_any") or ())
        unknown = (set(self.indicators) | self.requires_any) - set(INDICATOR_KEYS)
        if unknown:
            raise ValueError(f"규칙 '{self.name}': 모르는 지표 키 {sorted(unknown)}")
        if self.category not in CATEGORIES:
            raise ValueError(f"규칙 '{self.name}': 모르는 분류 {self.category!r}")


class Config:
    """indicator_keywords.json을 컴파일한 결과."""

    def __init__(self, data: dict):
        self.masks = tuple(m for m in data.get("mask", []) if m)
        self.rules = tuple(_Rule(r) for r in data["rules"])
        self.holdings = tuple((str(h["name"]), _Matcher(h["keywords"]))
                              for h in data.get("holdings", []))
        # 매체명은 대소문자 표기가 제각각이라(NewsData source_id는 전부 소문자) 늘 무시.
        self.official = _Matcher(t.lower() for t in data.get("official_sources", []))
        self.weights = dict(data["weights"])
        self.priority = tuple(data["category_priority"])
        if set(self.priority) != set(CATEGORIES):
            raise ValueError(f"category_priority는 {CATEGORIES}를 정확히 담아야 한다")

    def covered_indicators(self) -> set:
        return {k for r in self.rules for k in r.indicators}


@lru_cache(maxsize=None)
def load(path: str = str(KEYWORDS_PATH)) -> Config:
    return Config(json.loads(Path(path).read_text(encoding="utf-8")))


def _mask(text: str, masks: Sequence[str]) -> str:
    for m in masks:
        if m in text:
            text = text.replace(m, " ")
    return text


def score_article(a: Article, *, now: Optional[datetime] = None,
                  cfg: Optional[Config] = None) -> Article:
    """기사 하나에 indicators·rel_category·score를 붙인다(제자리 수정, 같은 객체 반환)."""
    cfg = cfg or load()
    w = cfg.weights
    t_raw = _mask(a.title or "", cfg.masks)
    b_raw = _mask(a.summary or "", cfg.masks)
    t_low, b_low = t_raw.lower(), b_raw.lower()

    def where(m: _Matcher) -> Optional[str]:
        if m.search(t_raw, t_low):
            return "title"
        if m.search(b_raw, b_low):
            return "body"
        return None

    hits: List[Tuple[_Rule, str]] = []
    deferred: List[Tuple[_Rule, str]] = []
    for rule in cfg.rules:
        if rule.unless is not None and where(rule.unless):
            continue
        place = where(rule.match)
        if place is None:
            continue
        (deferred if rule.requires_any else hits).append((rule, place))
    found = {k for r, _ in hits for k in r.indicators}
    # 지정학처럼 조건부인 규칙은 다른 규칙이 해당 지표(유가·환율·금리)를 맞혔을 때만.
    for rule, place in deferred:
        if found & rule.requires_any:
            hits.append((rule, place))
            found.update(rule.indicators)

    keyword = min(w["keyword_cap"],
                  sum(w["title_hit"] if p == "title" else w["body_hit"] for _, p in hits))
    held = {where(m) for _, m in cfg.holdings}
    holding = (w["holding_title"] if "title" in held
               else w["holding_body"] if "body" in held else 0.0)

    cats = {r.category for r, _ in hits}
    if holding:
        cats.add("holding")
    category = next((c for c in cfg.priority if c in cats), "")

    multi = w["multi_source"] if len(a.related) >= w["multi_source_min_related"] else 0.0
    src = (a.source_name or "").lower()
    official = w["official_source"] if src and cfg.official.search(src, src) else 0.0
    age_penalty = 0.0
    if a.published_at is not None:
        now = now or datetime.now(timezone.utc)
        age_h = max(0.0, (now - a.published_at).total_seconds() / 3600.0)
        age_penalty = min(w["age_penalty_max"], age_h / 24.0 * w["age_penalty_per_day"])

    a.indicators = sorted(found, key=_KEY_ORDER.__getitem__)
    a.rel_category = category
    a.score = round(keyword + holding + multi + official - age_penalty, 2)
    return a


def score_all(articles: Iterable[Article], *, now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    cfg = load()
    for a in articles:
        score_article(a, now=now, cfg=cfg)


def filter_relevant(articles: List[Article], *,
                    now: Optional[datetime] = None) -> Tuple[List[Article], int]:
    """채점 후 필수 분류에 하나도 안 맞는 기사를 버린다. (kept, dropped_count)."""
    score_all(articles, now=now)
    kept = [a for a in articles if a.rel_category]
    return kept, len(articles) - len(kept)


def clean_indicators(value) -> Optional[List[str]]:
    """LLM이 준 indicators 정리: 알려진 키만 남기고 INDICATOR_KEYS 순서로 정렬한다.

    키가 없거나(null 포함) 유효 키가 하나도 없으면 None — 호출자는 결정론 채점이
    붙인 indicators를 그대로 둔다."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    keys = {str(v).strip() for v in value} & set(INDICATOR_KEYS)
    if not keys:
        return None
    return sorted(keys, key=_KEY_ORDER.__getitem__)
