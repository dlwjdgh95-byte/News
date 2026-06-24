"""Deterministic fallback path — no LLM, no dedup analysis, no translation.

This is the safety net. If the main agent pipeline fails, times out, or returns
empty, this guarantees a minimal briefing still reaches Telegram at 07:30 KST.

It does the simplest possible thing: pull the freshest ~10 headline+link items
from each source group's RSS feeds and format them as a plain "오늘 주요 뉴스"
message, prefixed with "[폴백 모드]".
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from . import config, http, feeds, state, dedup
from .model import Article, SOURCE_A, SOURCE_B, SOURCE_C

KST = timezone(timedelta(hours=9))
GATHER_DEADLINE = 45  # hard ceiling for all fallback fetches combined


def _fresh(entries, n: int):
    def key(e):
        return e.published or datetime(1970, 1, 1, tzinfo=timezone.utc)
    return sorted(entries, key=key, reverse=True)[:n]


def _gather(feed_list, n: int, sent_log: Optional[dict] = None) -> List:
    """Fetch all feeds in parallel under a hard deadline so a single slow feed
    can never stall the fallback. Drops entries already pushed previously."""
    items = []
    with ThreadPoolExecutor(max_workers=min(8, len(feed_list) or 1)) as ex:
        futures = {ex.submit(http.fetch_feed, url, name): name for name, url in feed_list}
        for fut in futures:
            try:
                entries = fut.result(timeout=GATHER_DEADLINE)
            except (FTimeout, Exception):  # noqa: BLE001 - skip slow/broken feed
                entries = None
            if not entries:
                continue
            if sent_log is not None:
                entries = [e for e in entries
                           if not state.already_sent(sent_log, dedup.canonical_url(e.link))]
            items.extend(entries)
    return _fresh(items, n)


def build_fallback_message(now: datetime | None = None,
                           sent_log: Optional[dict] = None) -> str:
    now = now or datetime.now(KST)
    date_str = now.astimezone(KST).strftime("%Y-%m-%d (%a)")

    groups = [
        ("🗞 시사·국제", feeds.SOURCE_A_FEEDS),
        ("💹 경제·시장", feeds.SOURCE_B_FALLBACK_FEEDS),
        ("🪙 크립토", feeds.SOURCE_C_FEEDS),
    ]

    lines = [f"[폴백 모드] 오늘 주요 뉴스 — {date_str}", ""]
    any_items = False
    for header, feed_list in groups:
        items = _gather(feed_list, config.FALLBACK_ITEMS, sent_log)
        lines.append(header)
        if not items:
            lines.append("· 데이터 수집 실패")
        else:
            any_items = True
            for e in items:
                title = e.title.strip()
                if e.link:
                    lines.append(f"· {title}\n  {e.link}")
                else:
                    lines.append(f"· {title}")
        lines.append("")

    if not any_items:
        lines.append("⚠️ 모든 소스 수집에 실패했습니다. 네트워크/피드 상태를 확인하세요.")
    return "\n".join(lines).strip()


def run_fallback(send: bool = True, sent_log: Optional[dict] = None) -> str:
    """Build and (optionally) send the fallback briefing. Returns the message.

    ``sent_log`` (if given) is used to drop items already pushed previously, so a
    fallback after a successful prior run does not re-send stale headlines."""
    msg = build_fallback_message(sent_log=sent_log)
    if send:
        from . import telegram
        telegram.send_message(msg)
    return msg
