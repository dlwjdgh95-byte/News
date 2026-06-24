"""Source C collector — 크립토·핀테크 (crypto & fintech).

This is a SUPPLEMENTARY source: it pulls from a handful of official crypto/
fintech outlet RSS feeds (plus a keyless Google News search feed). Because it is
lower-trust and can be noisy, every item gets a modest confidence prior and the
total output is capped so it cannot flood the downstream pipeline.

This module does ONLY collection + mapping onto the ``Article`` model. Dedup,
filtering, translation and summarization are downstream stages and must NOT
happen here.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from ..model import Article, SOURCE_C
from .. import http, config, feeds  # noqa: F401  (config kept for parity/future use)

# Supplementary feeds are lower-trust than named primary outlets.
_SUPPLEMENTARY_CONFIDENCE = 0.55

# Cap the total items so a noisy supplementary source can't flood the pipeline.
_MAX_ITEMS = 20

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: Optional[str]) -> str:
    """Remove HTML tags + collapse whitespace from an RSS snippet.

    Deliberately small and dependency-free: feed summaries contain only simple
    markup (links, <b>, <p>), so a tag-stripping regex is sufficient here.
    """
    if not text:
        return ""
    cleaned = _TAG_RE.sub(" ", text)
    # Unescape the handful of entities feeds actually emit.
    cleaned = (
        cleaned.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return _WS_RE.sub(" ", cleaned).strip()


def _sort_key(article: Article) -> datetime:
    """Most-recent-first sort key; items with no date sort last."""
    pub = article.published_at
    if pub is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if pub.tzinfo is None:  # belt-and-suspenders; model also normalises this
        pub = pub.replace(tzinfo=timezone.utc)
    return pub


def collect() -> List[Article]:
    """Collect Source C (crypto/fintech) articles.

    Defensive by design: any single feed failing to fetch/parse leaves the rest
    intact, and the whole function never raises — on total failure it returns an
    empty list. Output is sorted newest-first and capped at ``_MAX_ITEMS``.
    """
    articles: List[Article] = []

    try:
        for feed_name, url in feeds.SOURCE_C_FEEDS:
            try:
                entries = http.fetch_feed(url, feed_name)
            except Exception:
                # A single broken/unreachable feed must never abort the others.
                entries = None
            if not entries:  # None => broken/invalid/empty feed; skip it.
                continue

            for entry in entries:
                try:
                    title = (entry.title or "").strip()
                    link = (entry.link or "").strip()
                    if not title or not link:
                        continue  # spec: skip empty title/url
                    articles.append(
                        Article(
                            title=title,
                            url=link,
                            source_name=(entry.source_name or feed_name or "").strip(),
                            source_tag=SOURCE_C,
                            summary=strip_html(entry.summary),
                            published_at=entry.published,
                            category="crypto",
                            language="",  # unknown — normalize stage detects it
                            confidence=_SUPPLEMENTARY_CONFIDENCE,
                        )
                    )
                except Exception:
                    # One malformed entry must not sink the whole feed.
                    continue

        # Newest first; dateless items fall to the end. Cap to avoid flooding.
        articles.sort(key=_sort_key, reverse=True)
        return articles[:_MAX_ITEMS]
    except Exception:
        # Total failure => degrade gracefully rather than crash the pipeline.
        return []
