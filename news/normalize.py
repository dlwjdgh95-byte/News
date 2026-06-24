"""Normalisation: turn raw collected Articles into clean common-model items.

Runs right after collection, before pre-filtering. Responsibilities:
- collapse whitespace / strip stray HTML left in titles & summaries,
- detect language (cheap heuristic: Hangul presence => Korean),
- ensure published_at is tz-aware UTC,
- drop obviously empty items.

It does NOT dedup or filter by topic — those are later, dedicated stages.
"""

from __future__ import annotations

import re
from datetime import timezone
from typing import List

from .model import Article

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HANGUL_RE = re.compile(r"[가-힣]")


def _clean(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return _WS_RE.sub(" ", text).strip()


def detect_language(article: Article) -> str:
    if article.language:
        return article.language
    sample = f"{article.title} {article.summary}"
    return "ko" if _HANGUL_RE.search(sample) else "en"


def normalize(articles: List[Article]) -> List[Article]:
    out: List[Article] = []
    for a in articles:
        a.title = _clean(a.title)
        a.summary = _clean(a.summary)
        a.original_title = _clean(a.original_title) or a.title
        if not a.title or not a.url:
            continue
        a.language = detect_language(a)
        if a.published_at is not None and a.published_at.tzinfo is None:
            a.published_at = a.published_at.replace(tzinfo=timezone.utc)
        out.append(a)
    return out
