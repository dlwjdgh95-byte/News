"""Shared HTTP + feed-parsing utilities.

Reliability guards from the spec live here:
- retries with exponential backoff,
- validating that an RSS URL actually returns parseable XML before use,
- detecting "silent failures" (HTTP 200 but empty/garbage body) so callers can
  skip a broken feed and let the pipeline fall through.

We rely only on ``requests`` + stdlib ``xml.etree`` so the system runs on a
freshly-cloned container with a minimal install.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from xml.etree import ElementTree as ET

import requests

from . import config

_session: Optional[requests.Session] = None


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "*/*"})
        _session = s
    return _session


def get(url: str, *, params: dict | None = None, timeout: float | None = None,
        retries: int | None = None, headers: dict | None = None) -> Optional[requests.Response]:
    """GET with exponential-backoff retries. Returns None on persistent failure."""
    timeout = timeout or config.HTTP_TIMEOUT
    retries = config.HTTP_RETRIES if retries is None else retries
    delay = 2.0
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = session().get(url, params=params, timeout=timeout, headers=headers)
            if resp.status_code == 200:
                return resp
            # 4xx (other than 429) won't fix themselves — don't burn retries.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                return None
        except requests.RequestException as exc:  # noqa: PERF203
            last_exc = exc
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    if last_exc is not None:
        # Surfaced via return None; callers treat as a soft failure.
        pass
    return None


def get_json(url: str, *, params: dict | None = None, headers: dict | None = None) -> Optional[dict]:
    resp = get(url, params=params, headers=headers)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Feed parsing
# --------------------------------------------------------------------------
@dataclass
class FeedEntry:
    title: str
    link: str
    summary: str
    published: Optional[datetime]
    source_name: str


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    raw = raw.strip()
    # RFC 822 (RSS) e.g. "Tue, 24 Jun 2026 06:30:00 GMT"
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    # ISO 8601 (Atom) e.g. "2026-06-24T06:30:00Z"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed_xml(xml_bytes: bytes, default_source: str = "") -> Optional[List[FeedEntry]]:
    """Parse RSS 2.0 or Atom. Returns None if the body is not valid feed XML."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    tag = _strip_ns(root.tag)
    entries: List[FeedEntry] = []

    if tag == "rss" or root.find("channel") is not None:
        channel = root.find("channel")
        if channel is None:
            return None
        feed_title = _text(channel.find("title")) or default_source
        for item in channel.findall("item"):
            link = _text(item.find("link"))
            entries.append(FeedEntry(
                title=_text(item.find("title")),
                link=link,
                summary=_text(item.find("description")),
                published=_parse_date(_text(item.find("pubDate"))),
                source_name=_extract_source(item, feed_title),
            ))
        return entries

    if tag == "feed":  # Atom
        feed_title = ""
        for child in root:
            if _strip_ns(child.tag) == "title":
                feed_title = _text(child)
                break
        feed_title = feed_title or default_source
        for entry in root:
            if _strip_ns(entry.tag) != "entry":
                continue
            title = link = summary = pub = ""
            for child in entry:
                name = _strip_ns(child.tag)
                if name == "title":
                    title = _text(child)
                elif name == "link":
                    link = child.get("href", "") or link
                elif name in ("summary", "content"):
                    summary = _text(child) or summary
                elif name in ("published", "updated"):
                    pub = _text(child) or pub
            entries.append(FeedEntry(title, link, summary, _parse_date(pub), feed_title))
        return entries

    return None


def _extract_source(item: ET.Element, default: str) -> str:
    """Google News RSS puts the real outlet in a <source> element."""
    for child in item:
        if _strip_ns(child.tag) == "source":
            txt = _text(child)
            if txt:
                return txt
    return default


def fetch_feed(url: str, default_source: str = "") -> Optional[List[FeedEntry]]:
    """Fetch + validate a feed URL. Returns None for unreachable/invalid/empty
    feeds so the caller can skip them (no silent failures).

    Feeds are fetched with a short timeout and a single retry: a feed either
    responds quickly or we skip it — we must never let one slow feed blow the
    overall run budget."""
    resp = get(url, timeout=min(config.HTTP_TIMEOUT, 10), retries=1)
    if resp is None or not resp.content:
        return None
    entries = parse_feed_xml(resp.content, default_source)
    if not entries:  # empty or unparseable => treat as broken
        return None
    return entries
