"""Source A collector — 시사·정치·국제·한국 (current affairs, politics, international, Korea).

Primary source is keyless Google News RSS (always available). When a Guardian
API key is configured we additionally pull a few sections as a quality boost.

This module does ONLY collection + mapping onto the ``Article`` model. Dedup,
filtering, translation and summarization are downstream stages.
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..model import Article, SOURCE_A
from .. import http, config, feeds

# Confidence priors per the spec.
_AGGREGATOR_CONFIDENCE = 0.6   # Google News RSS aggregates many outlets.
_GUARDIAN_CONFIDENCE = 0.8     # Named, reputable outlet.

_GUARDIAN_SEARCH_URL = "https://content.guardianapis.com/search"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: Optional[str]) -> str:
    """Remove HTML tags + collapse whitespace from an RSS/API snippet.

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


def _category_for_feed(feed_name: str, url: str) -> str:
    """Derive a coarse category from which Google News feed an item came from."""
    u = url.upper()
    if "TOPIC/POLITICS" in u:
        return "politics"
    if "TOPIC/WORLD" in u:
        return "world"
    if "TOPIC/NATION" in u:
        return "nation"
    return "general"


def _collect_rss() -> List[Article]:
    """Pull every feed in ``feeds.SOURCE_A_FEEDS`` and map to Articles."""
    articles: List[Article] = []
    for feed_name, url in feeds.SOURCE_A_FEEDS:
        try:
            entries = http.fetch_feed(url, feed_name)
        except Exception:
            # A single broken/unreachable feed must never abort the others.
            entries = None
        if not entries:
            continue
        category = _category_for_feed(feed_name, url)
        for entry in entries:
            title = (entry.title or "").strip()
            link = (entry.link or "").strip()
            if not title or not link:
                continue  # spec: skip empty title/url
            articles.append(
                Article(
                    title=title,
                    url=link,
                    source_name=(entry.source_name or feed_name or "").strip(),
                    source_tag=SOURCE_A,
                    summary=strip_html(entry.summary),
                    published_at=entry.published,
                    category=category,
                    language="",  # unknown — normalize stage detects it
                    confidence=_AGGREGATOR_CONFIDENCE,
                )
            )
    return articles


def _collect_guardian() -> List[Article]:
    """Optional Guardian Content API quality boost. Silent no-op on any failure."""
    api_key = config.GUARDIAN_API_KEY
    if not api_key:
        return []

    articles: List[Article] = []
    for section in feeds.GUARDIAN_SECTIONS_A:
        try:
            data = http.get_json(
                _GUARDIAN_SEARCH_URL,
                params={
                    "section": section,
                    "api-key": api_key,
                    "show-fields": "trailText,byline",
                    "page-size": 10,
                    "order-by": "newest",
                },
            )
        except Exception:
            data = None
        if not data:
            continue

        results = (data.get("response") or {}).get("results") or []
        if not isinstance(results, list):
            continue

        for item in results:
            if not isinstance(item, dict):
                continue
            title = (item.get("webTitle") or "").strip()
            link = (item.get("webUrl") or "").strip()
            if not title or not link:
                continue
            fields = item.get("fields") or {}
            summary = strip_html(fields.get("trailText"))
            published = http._parse_date(item.get("webPublicationDate") or "")
            articles.append(
                Article(
                    title=title,
                    url=link,
                    source_name="The Guardian",
                    source_tag=SOURCE_A,
                    summary=summary,
                    published_at=published,
                    category=section,
                    language="en",
                    confidence=_GUARDIAN_CONFIDENCE,
                )
            )
    return articles


def collect() -> List[Article]:
    """Collect Source A articles (Google News RSS + optional Guardian boost).

    Defensive by design: any single feed or the whole Guardian call failing
    leaves the rest of the results intact. Returns raw, un-deduplicated,
    un-filtered Articles for downstream stages to process.
    """
    articles: List[Article] = []

    try:
        articles.extend(_collect_rss())
    except Exception:
        pass

    try:
        articles.extend(_collect_guardian())
    except Exception:
        pass

    return articles
