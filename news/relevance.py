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

키워드 매칭 규칙(``news/indicator_keywords.json``):
  · 한글이 섞인 키워드는 부분 문자열로 맞춘다. 짧은 한국어 낱말은 표식으로 경계를
    건다 — 앞에 ``^``: 한글 음절(또는 한글에 붙은 닫는 따옴표·괄호) 바로 뒤에서는
    안 맞는다('^한은'은 '한은'·'[한은]'에 맞고 '북한은'·'시한은'·'권한은'에는 안
    맞는다). 뒤에 ``$``: 한글 음절이 바로 이어지면 안 맞는다('^가자$'는 '가자 휴전'에
    맞고 '가자에서'·'참가자'에는 안 맞는다). 조사가 붙는 낱말에는 ``$``를 쓰지 않는다.
  · 영문 키워드는 영숫자 경계로 맞춘다 — 'oil'이 'turmoil'에, 'eth'가 'ethics'에
    걸리지 않는다. 한글 조사가 바로 붙어도('FOMC는') 맞는다(``\\b``는 한글을 단어
    문자로 봐서 이 경우를 놓친다).
  · 대소문자는 기본으로 무시한다 — 제목식 대문자('U.S. Dollar', 'Clarity Act',
    'Gold ETF')와 전부 대문자('NASDAQ')도 맞는다. 예외는 JSON 최상위
    ``case_sensitive`` 목록의 약어뿐이다: 소문자로 쓰면 흔한 다른 낱말이 되는 것
    (TIPS≠tips, HOOD≠hood, Fed≠fed up, SEC≠sec, QT·QE, ISM≠-ism, JOLTS≠jolts,
    BOK≠bok choy, RBI≠rbi, US≠us)과 늘 대문자로만 쓰는 티커 ETH. 이들은 정확히 그
    표기로만 맞는다.
  · 전역 ``mask`` 낱말(유가족·유가증권·palm oil·gold medal 등)은 모든 매칭 전에
    지운다. 규칙별 ``mask``는 그 규칙의 키워드를 맞추기 전에만 지운다(관세청 ⊃ 관세).
    영문 키워드·mask 앞에 ``*``를 붙이면 앞쪽 영숫자 경계를 따지지 않는다
    ('*billion-dollar'는 'multibillion-dollar' 안에서도 지워진다). 거래소 티커
    표기(``NASDAQ:MU``·``(NYSE: BRK)``)는 코드의 고정 패턴으로 지운다 — 대문자
    거래소명 + 콜론 + 대문자 티커일 때만.
  · 규칙별 ``unless``(한은·해외 중앙은행 → 연준 키 금지): 제목에 보이면 그 기사
    전체에서, 스니펫에만 보이면 스니펫에서만 규칙을 건너뛴다 — '이창용 …' 제목의
    스니펫 '금리 인하'도 연준으로 새지 않고, '美 금리 인하' 제목은 스니펫에 '한은'이
    스쳐도 연준 지표를 단다. ``@규칙이름``은 그 규칙의 키워드 전부를 뜻한다.
  · 규칙별 ``context``: 키워드가 맞은 필드에 이 낱말도 있어야 규칙이 선다(제목의
    맥락은 스니펫에도 통한다 — 제목이 기사의 주어다). 노동시장·구인 같은 일반 낱말은
    美·미국·연준 등 미국 맥락일 때만 claims이고, '국내 고용시장' 제목은 스니펫에
    '미국 관세'가 나와도 claims가 아니다.
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

# 한국어 경계 표식 — ``^``(앞)·``$``(뒤). 앞 경계는 한글 음절 바로 뒤가 아니고, 한글에
# 붙은 닫는 따옴표·괄호 바로 뒤도 아니어야 한다("'긴축'이란"의 '이란'은 조사다).
# 여는 따옴표 뒤(" '이란 핵시설'")는 낱말의 시작이라 맞는다.
_HANGUL = "가-힣"
_LEFT = rf"(?<![{_HANGUL}])(?<![{_HANGUL}][\"'’”」』)\]])"
_RIGHT = rf"(?![{_HANGUL}])"
_ASCII_LEFT = r"(?<![0-9A-Za-z])"
_ASCII_RIGHT = r"(?![0-9A-Za-z])"


def _pattern(term: str) -> str:
    """키워드 하나 → 정규식 조각. 영문은 영숫자 경계(앞에 ``*``면 앞 경계 없음),
    한글은 표식(^·$)대로."""
    if term.isascii():
        free_left = term.startswith("*")
        if free_left:
            term = term[1:]
        # 경계는 키워드 가장자리가 영숫자일 때만 건다.
        return ((_ASCII_LEFT if term[0].isalnum() and not free_left else "")
                + re.escape(term) + (_ASCII_RIGHT if term[-1].isalnum() else ""))
    left = term.startswith("^")
    right = term.endswith("$")
    core = term[1 if left else 0:len(term) - 1 if right else len(term)]
    return (_LEFT if left else "") + re.escape(core) + (_RIGHT if right else "")


def _alternation(terms: Sequence[str], flags: int):
    if not terms:
        return None
    body = "|".join(_pattern(t) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(body, flags)


# 거래소 티커 표기 — 'NASDAQ:MU'·'(NYSE: BRK.B)'. 종목 기사의 꼬리표일 뿐 미 증시
# 기사가 아니다. 대문자 거래소명 + 콜론 + 대문자 티커만 지워서 '…on the Nasdaq: Apple
# up 2%'의 Nasdaq은 그대로 미 증시 규칙에 맞는다.
_TICKER = re.compile(r"(?<![A-Za-z])(?:NASDAQ|NYSE)\s?:\s?[A-Z]{1,5}(?![A-Za-z])")


class _Matcher:
    """키워드 목록 하나를 컴파일한 매처. 규칙은 모듈 docstring 참고."""

    __slots__ = ("_cased", "_uncased")

    def __init__(self, terms: Iterable[str], case_sensitive: frozenset = frozenset()):
        terms = [t.strip() for t in terms if t and t.strip()]
        self._cased = _alternation([t for t in terms if t in case_sensitive], 0)
        self._uncased = _alternation([t for t in terms if t not in case_sensitive],
                                     re.IGNORECASE)

    def search(self, text: str) -> bool:
        if self._cased is not None and self._cased.search(text):
            return True
        return self._uncased is not None and self._uncased.search(text) is not None


class _Masker:
    """mask 낱말을 지우는 치환기(대소문자 무시, 영문은 영숫자 경계)."""

    __slots__ = ("_re", "_tickers")

    def __init__(self, terms: Iterable[str], *, tickers: bool = False):
        self._re = _alternation([t.strip() for t in terms if t and t.strip()], re.IGNORECASE)
        self._tickers = tickers

    def __call__(self, text: str) -> str:
        if not text:
            return text
        if self._tickers:
            text = _TICKER.sub(" ", text)
        return self._re.sub(" ", text) if self._re is not None else text


class _Rule:
    __slots__ = ("name", "category", "indicators", "match", "unless", "mask",
                 "context", "requires_any")

    def __init__(self, spec: dict, case_sensitive: frozenset, keywords_of: dict):
        self.name = str(spec.get("name", ""))
        self.category = spec["category"]
        self.indicators = tuple(spec.get("indicators") or ())
        self.match = _Matcher(spec["keywords"], case_sensitive)
        unless: List[str] = []
        for t in spec.get("unless") or ():
            if t.startswith("@"):
                if t[1:] not in keywords_of:
                    raise ValueError(f"규칙 '{self.name}': unless가 가리키는 규칙 {t!r}이 없다")
                unless.extend(keywords_of[t[1:]])
            else:
                unless.append(t)
        self.unless = _Matcher(unless, case_sensitive) if unless else None
        self.mask = _Masker(spec.get("mask") or ())
        self.context = (_Matcher(spec["context"], case_sensitive)
                        if spec.get("context") else None)
        self.requires_any = frozenset(spec.get("requires_any") or ())
        unknown = (set(self.indicators) | self.requires_any) - set(INDICATOR_KEYS)
        if unknown:
            raise ValueError(f"규칙 '{self.name}': 모르는 지표 키 {sorted(unknown)}")
        if self.category not in CATEGORIES:
            raise ValueError(f"규칙 '{self.name}': 모르는 분류 {self.category!r}")

    def place(self, fields: Sequence[Tuple[str, str]]) -> Optional[str]:
        """처음 맞은 필드 이름("title"/"body") 또는 None. fields는 (제목, 스니펫) 순.

        unless: 제목에 보이면 기사 전체에서 건너뛴다(제목이 한은·해외 중앙은행
        기사면 스니펫의 '금리 인하'도 그 은행 얘기다). 스니펫에만 보이면 스니펫만
        건너뛴다 — 제목의 '美 금리 인하'는 스니펫에 '한은'이 스쳐도 맞는다."""
        title_ctx = (self.context is not None and bool(fields) and bool(fields[0][1])
                     and self.context.search(fields[0][1]))
        for i, (name, text) in enumerate(fields):
            if not text:
                continue
            if self.unless is not None and self.unless.search(text):
                if i == 0:
                    return None
                continue
            if not self.match.search(self.mask(text)):
                continue
            # context: 맞은 필드에 맥락이 있거나, 제목에 맥락이 있을 때만.
            if self.context is not None and not (title_ctx or self.context.search(text)):
                continue
            return name
        return None


class Config:
    """indicator_keywords.json을 컴파일한 결과."""

    def __init__(self, data: dict):
        cs = frozenset(data.get("case_sensitive", ()))
        self.mask = _Masker(data.get("mask", ()), tickers=True)
        keywords_of = {str(r.get("name", "")): list(r["keywords"]) for r in data["rules"]}
        self.rules = tuple(_Rule(r, cs, keywords_of) for r in data["rules"])
        self.holdings = tuple((str(h["name"]), _Matcher(h["keywords"], cs))
                              for h in data.get("holdings", []))
        # 매체명은 대소문자 표기가 제각각이라(NewsData source_id는 전부 소문자) 늘 무시.
        self.official = _Matcher(data.get("official_sources", []))
        self.weights = dict(data["weights"])
        self.priority = tuple(data["category_priority"])
        if set(self.priority) != set(CATEGORIES):
            raise ValueError(f"category_priority는 {CATEGORIES}를 정확히 담아야 한다")

    def covered_indicators(self) -> set:
        return {k for r in self.rules for k in r.indicators}


@lru_cache(maxsize=None)
def load(path: str = str(KEYWORDS_PATH)) -> Config:
    return Config(json.loads(Path(path).read_text(encoding="utf-8")))


def score_article(a: Article, *, now: Optional[datetime] = None,
                  cfg: Optional[Config] = None) -> Article:
    """기사 하나에 indicators·rel_category·score를 붙인다(제자리 수정, 같은 객체 반환)."""
    cfg = cfg or load()
    w = cfg.weights
    fields = (("title", cfg.mask(a.title or "")), ("body", cfg.mask(a.summary or "")))

    def where(m: _Matcher) -> Optional[str]:
        return next((name for name, text in fields if text and m.search(text)), None)

    hits: List[Tuple[_Rule, str]] = []
    deferred: List[Tuple[_Rule, str]] = []
    for rule in cfg.rules:
        place = rule.place(fields)
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
    src = a.source_name or ""
    official = w["official_source"] if src and cfg.official.search(src) else 0.0
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

    · 명시적인 빈 목록(``[]``) → ``[]`` — 모델이 "움직일 지표 없음"이라고 판정한 것이라
      결정론 태그를 지운다.
    · 키가 없거나(null 포함) 목록에 유효 키가 하나도 없으면(오타·모르는 키뿐) None —
      호출자는 결정론 채점이 붙인 indicators를 그대로 둔다."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    if not value:
        return []
    keys = {str(v).strip() for v in value} & set(INDICATOR_KEYS)
    if not keys:
        return None
    return sorted(keys, key=_KEY_ORDER.__getitem__)
