"""Source B collector — 경제·증시 (economy & stock-market news).

Primary source is NewsData.io (business category, ko+en). NewsData exposes a
``sentiment`` field which we map onto ``Article.sentiment`` — this feeds the
"오늘 시장 분위기" mood summary downstream, so we preserve it faithfully.

Credit discipline is the dominant design constraint: NewsData free credits are
scarce, so this module makes at most ``config.NEWSDATA_MAX_REQUESTS`` paginated
requests per run, caches identical queries for the day, and *never* spends a
NewsData request inside the fallback path.

Fallback order when NewsData is unavailable / errored / empty:
    1. Guardian business section (if GUARDIAN_API_KEY set)
    2. Google News RSS business feeds (``feeds.SOURCE_B_FALLBACK_FEEDS``)

Collectors only collect + map. No dedup / filter / translate / summarize here.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from ..model import Article, SOURCE_B
from .. import http, config, feeds, state

NEWSDATA_URL = "https://newsdata.io/api/1/news"

# Markets-focused query; kept simple so the cache key stays stable day to day.
NEWSDATA_QUERY = "증시 OR 코스피 OR 환율 OR 주식 OR stock market OR economy"

# Cache TTL: one briefing day. An identical query within the day reuses the
# cached raw payload instead of re-spending NewsData credits.
_CACHE_TTL_SECONDS = 24 * 3600

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_VALID_SENTIMENTS = {"positive", "neutral", "negative"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    # Collapse a few common HTML entities + whitespace.
    text = (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&nbsp;", " "))
    return _WS_RE.sub(" ", text).strip()


def _parse_newsdata_date(raw: str) -> Optional[datetime]:
    """NewsData pubDate looks like '2026-06-24 06:30:00' (UTC). Be defensive."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm_sentiment(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    return s if s in _VALID_SENTIMENTS else None


def _norm_category(raw: object) -> str:
    """NewsData category is a list like ['business']; default to 'business'."""
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, str) and c.strip():
                return c.strip().lower()
    elif isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    return "business"


def _safe_str(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


# --------------------------------------------------------------------------
# NewsData.io (primary)
# --------------------------------------------------------------------------
def _newsdata_to_article(item: dict) -> Optional[Article]:
    if not isinstance(item, dict):
        return None
    title = _safe_str(item.get("title"))
    url = _safe_str(item.get("link"))
    if not title or not url:
        return None
    source_name = _safe_str(item.get("source_id")) or "NewsData"
    return Article(
        title=title,
        url=url,
        source_name=source_name,
        source_tag=SOURCE_B,
        summary=_strip_html(_safe_str(item.get("description"))),
        published_at=_parse_newsdata_date(_safe_str(item.get("pubDate"))),
        category=_norm_category(item.get("category")),
        sentiment=_norm_sentiment(item.get("sentiment")),
        language=_safe_str(item.get("language")),
        confidence=0.7,
    )


def _collect_newsdata() -> List[Article]:
    """Primary path. Returns [] (never raises) on any error/empty/rate-limit.

    Enforces the credit cap with a hard request counter: at most
    ``config.NEWSDATA_MAX_REQUESTS`` HTTP calls, paginating via ``nextPage``.
    A served-from-cache page does NOT consume a request.
    """
    if not config.NEWSDATA_API_KEY:
        return []

    max_requests = max(0, int(config.NEWSDATA_MAX_REQUESTS))
    page_size = max(1, int(config.NEWSDATA_PAGE_SIZE))

    articles: List[Article] = []
    requests_made = 0
    next_page: Optional[str] = None

    while requests_made < max_requests:
        params = {
            "apikey": config.NEWSDATA_API_KEY,
            "category": "business",
            "language": "ko,en",
            "q": NEWSDATA_QUERY,
            "size": page_size,
        }
        if next_page:
            params["page"] = next_page

        # Cache key excludes the apikey but pins the query + page so identical
        # queries within the day are served without spending a credit.
        cache_key = f"newsdata_b|q={NEWSDATA_QUERY}|size={page_size}|page={next_page or ''}"
        data = state.cache_get(cache_key, ttl_seconds=_CACHE_TTL_SECONDS)

        if data is None:
            # Cache miss => spend exactly one request.
            data = http.get_json(NEWSDATA_URL, params=params)
            requests_made += 1
            if not isinstance(data, dict) or data.get("status") != "success":
                # Error / rate-limit / garbage => abandon NewsData entirely.
                break
            state.cache_set(cache_key, data)

        results = data.get("results")
        if not isinstance(results, list) or not results:
            break

        for item in results:
            art = _newsdata_to_article(item)
            if art is not None:
                articles.append(art)

        next_page = data.get("nextPage") if isinstance(data.get("nextPage"), str) else None
        if not next_page:
            break

    return articles


# --------------------------------------------------------------------------
# Guardian (fallback 1) — never spends NewsData credits
# --------------------------------------------------------------------------
def _collect_guardian() -> List[Article]:
    if not config.GUARDIAN_API_KEY:
        return []
    articles: List[Article] = []
    for section in getattr(feeds, "GUARDIAN_SECTIONS_B", ["business"]):
        params = {
            "section": section,
            "api-key": config.GUARDIAN_API_KEY,
            "show-fields": "trailText",
            "page-size": config.FALLBACK_ITEMS,
            "order-by": "newest",
        }
        data = http.get_json("https://content.guardianapis.com/search", params=params)
        if not isinstance(data, dict):
            continue
        response = data.get("response")
        if not isinstance(response, dict) or response.get("status") != "ok":
            continue
        for item in response.get("results") or []:
            if not isinstance(item, dict):
                continue
            title = _safe_str(item.get("webTitle"))
            url = _safe_str(item.get("webUrl"))
            if not title or not url:
                continue
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
            summary = _strip_html(_safe_str(fields.get("trailText")))
            published_at = None
            raw_date = _safe_str(item.get("webPublicationDate"))
            if raw_date:
                try:
                    published_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except ValueError:
                    published_at = None
            articles.append(Article(
                title=title,
                url=url,
                source_name="The Guardian",
                source_tag=SOURCE_B,
                summary=summary,
                published_at=published_at,
                category=_safe_str(item.get("sectionName")).lower() or "business",
                sentiment=None,
                language="en",
                confidence=0.7,
            ))
    return articles


# --------------------------------------------------------------------------
# Google News RSS (fallback 2) — never spends NewsData credits
# --------------------------------------------------------------------------
def _collect_rss() -> List[Article]:
    articles: List[Article] = []
    for source_label, url in getattr(feeds, "SOURCE_B_FALLBACK_FEEDS", []):
        entries = http.fetch_feed(url, default_source=source_label)
        if not entries:
            continue
        for entry in entries[: config.FALLBACK_ITEMS]:
            title = (entry.title or "").strip()
            link = (entry.link or "").strip()
            if not title or not link:
                continue
            articles.append(Article(
                title=title,
                url=link,
                source_name=(entry.source_name or source_label or "Google News").strip(),
                source_tag=SOURCE_B,
                summary=_strip_html(entry.summary or ""),
                published_at=entry.published,
                category="business",
                sentiment=None,
                language="ko",
                confidence=0.6,
            ))
    return articles


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def collect() -> List[Article]:
    """Collect Source B (경제·증시) articles. Never raises; [] on total failure."""
    try:
        primary = _collect_newsdata()
        if primary:
            return primary

        # Fallbacks must never spend a NewsData credit.
        guardian = _collect_guardian()
        if guardian:
            return guardian

        return _collect_rss()
    except Exception:
        # Defensive: a collector failure must not crash the pipeline.
        return []
