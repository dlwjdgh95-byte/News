"""Keyword hard-filter applied BEFORE the LLM and BEFORE dedup.

Goal (per spec): cut noise/cost before any LLM call by dropping entertainment /
celebrity / sports topics, while KEEPING lifestyle. Runs right after normalize
and before dedup.

This is intentionally a cheap, conservative keyword filter — it should only drop
items we are confident are off-topic. When unsure, keep the item (later stages
will down-rank it).
"""

from __future__ import annotations

import re
from typing import List, Tuple

from .model import Article

# Exclude: 연예·셀럽·스포츠. Bilingual cues.
_EXCLUDE = [
    # Sports
    "스포츠", "축구", "야구", "농구", "배구", "골프", "월드컵", "올림픽", "프로야구",
    "kbo", "epl", "k리그", "손흥민", "김연아",
    "sports", "soccer", "football", "baseball", "basketball", "nba", "nfl",
    "mlb", "premier league", "world cup", "olympic", "tennis", "golf tournament",
    # Entertainment / celebrity
    "연예", "아이돌", "케이팝", "k-pop", "kpop", "걸그룹", "보이그룹", "데뷔",
    "컴백", "예능", "드라마 시청률", "영화 흥행", "박스오피스", "열애", "결혼설",
    "이혼", "셀럽", "유튜버", "인플루언서",
    "celebrity", "celeb", "box office", "red carpet", "grammy", "oscars",
    "billboard", "dating rumor", "tour dates", "concert tour",
]

# Lifestyle is explicitly KEPT even though it can look "soft".
_KEEP_OVERRIDE = [
    "라이프", "라이프스타일", "건강", "여행", "음식", "트렌드", "소비", "부동산",
    "lifestyle", "health", "travel", "food", "wellness", "housing", "real estate",
]

def _compile(terms: List[str]):
    """Build a matcher for a term list.

    ASCII terms are matched with word boundaries so short tokens like "nfl",
    "nba", "mlb", "m", "bn" can NOT match inside unrelated words (the classic
    "nfl" ⊂ "inflation" bug that would drop economic news). CJK terms keep
    substring matching, which is the desired behaviour for Korean compounds
    (e.g. "연예" should match "연예인").
    """
    ascii_terms = sorted({t for t in terms if t.isascii()}, key=len, reverse=True)
    cjk_terms = [t for t in terms if not t.isascii()]
    ascii_re = None
    if ascii_terms:
        ascii_re = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in ascii_terms) + r")\b")
    return ascii_re, cjk_terms


_EXCLUDE_RE, _EXCLUDE_CJK = _compile(_EXCLUDE)
_KEEP_RE, _KEEP_CJK = _compile(_KEEP_OVERRIDE)


def _hay(article: Article) -> str:
    return f"{article.title} {article.summary} {article.category}".lower()


def _matches(hay: str, compiled) -> bool:
    ascii_re, cjk_terms = compiled
    if ascii_re is not None and ascii_re.search(hay):
        return True
    return any(t in hay for t in cjk_terms)


def prefilter(articles: List[Article]) -> Tuple[List[Article], int]:
    """Return (kept, dropped_count)."""
    kept: List[Article] = []
    dropped = 0
    for a in articles:
        hay = _hay(a)
        if _matches(hay, (_KEEP_RE, _KEEP_CJK)):
            kept.append(a)
            continue
        if _matches(hay, (_EXCLUDE_RE, _EXCLUDE_CJK)):
            dropped += 1
            continue
        kept.append(a)
    return kept, dropped
