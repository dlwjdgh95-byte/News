"""Deterministic fallback path — no LLM, no translation, no insight.

This is the safety net. If the main agent pipeline fails, times out, or returns
empty, this guarantees a minimal briefing still reaches Telegram, prefixed with
"[폴백 모드]".

Even without an LLM it applies the cheap deterministic hygiene the main path uses
— keyword pre-filter (drop 연예/스포츠) and multi-stage de-duplication — so a
fallback day still avoids the two most visible problems (celebrity/sports noise
and duplicate stories). Output is headline+link only, formatted with HTML for
readability.
"""

from __future__ import annotations

import html as _html
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from . import config, http, feeds, state, dedup, normalize, prefilter
from .model import Article, SOURCE_A, SOURCE_B, SOURCE_C

KST = timezone(timedelta(hours=9))
GATHER_DEADLINE = 45  # hard ceiling for all fallback fetches combined
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _entry_to_article(e, tag: str) -> Optional[Article]:
    if not e.title or not e.link:
        return None
    return Article(title=e.title, url=e.link, source_name=e.source_name or "",
                   source_tag=tag, summary=e.summary or "", published_at=e.published,
                   confidence=0.5)


def _gather(feed_list, tag: str, n: int, sent_log: Optional[dict] = None) -> List[Article]:
    """Fetch feeds in parallel under a hard deadline, then apply the same cheap
    hygiene the main path uses: normalize -> prefilter -> dedup -> drop-sent."""
    arts: List[Article] = []
    with ThreadPoolExecutor(max_workers=min(8, len(feed_list) or 1)) as ex:
        futures = {ex.submit(http.fetch_feed, url, name): name for name, url in feed_list}
        for fut in futures:
            try:
                entries = fut.result(timeout=GATHER_DEADLINE)
            except (FTimeout, Exception):  # noqa: BLE001 - skip slow/broken feed
                entries = None
            for e in entries or []:
                a = _entry_to_article(e, tag)
                if a is not None:
                    arts.append(a)

    arts = normalize.normalize(arts)
    arts, _dropped = prefilter.prefilter(arts)      # drop 연예/스포츠
    reps = dedup.deduplicate(arts)                  # drop duplicates
    if sent_log is not None:
        reps = [a for a in reps if not state.already_sent(sent_log, a.canonical_url)]
    reps.sort(key=lambda a: a.published_at or _EPOCH, reverse=True)
    return reps[:n]


def _fmt(a: Article) -> str:
    title = _html.escape(a.title, quote=False)
    src = f' <i>— {_html.escape(a.source_name, quote=False)}</i>' if a.source_name else ""
    if a.url:
        return f'· <a href="{_html.escape(a.url, quote=True)}">{title}</a>{src}'
    return f"· {title}{src}"


def build_fallback_message(now: datetime | None = None,
                           sent_log: Optional[dict] = None) -> str:
    now = now or datetime.now(KST)
    date_str = now.astimezone(KST).strftime("%Y-%m-%d (%a)")

    groups = [
        ("🗞 시사·국제", feeds.SOURCE_A_FEEDS, SOURCE_A),
        ("💹 경제·시장", feeds.SOURCE_B_FALLBACK_FEEDS, SOURCE_B),
        ("🪙 크립토", feeds.SOURCE_C_FEEDS, SOURCE_C),
    ]

    lines = [f"⚠️ <b>[폴백 모드] 오늘 주요 뉴스</b> — {date_str}",
             "<i>요약·분석 없이 핵심 헤드라인만 전달합니다.</i>", ""]
    any_items = False
    for header, feed_list, tag in groups:
        items = _gather(feed_list, tag, config.FALLBACK_ITEMS, sent_log)
        lines.append(f"<b>{header}</b>")
        if not items:
            lines.append("· 데이터 수집 실패")
        else:
            any_items = True
            lines.extend(_fmt(a) for a in items)
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
        telegram.send_message(msg)  # HTML-formatted
    return msg
