"""Render the Korean briefing.

Sections (per spec):
  · 오늘의 헤드라인 3줄 요약
  · 경제·시장 (지수·환율 자리 + 핵심 뉴스 + 감성 기반 시장 분위기 한 줄)
  · 시사·국제 (중립 톤)
  · 크립토 (보조)
  · 오늘 주목할 이벤트

Push vs archive: low-confidence or weak-evidence items are archived (kept in the
markdown file) but NOT pushed to Telegram.
"""

from __future__ import annotations

import html as _html
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

from .model import Article, SOURCE_A, SOURCE_B, SOURCE_C

KST = timezone(timedelta(hours=9))

_CIRCLED = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]


def _esc(s: str) -> str:
    """Escape text for Telegram HTML parse_mode."""
    return _html.escape(str(s), quote=False)


def _esc_attr(s: str) -> str:
    return _html.escape(str(s), quote=True)


def _html_to_md(s: str) -> str:
    """Convert the HTML briefing to Markdown for the committed archive file."""
    s = re.sub(r'<a href="([^"]*)">(.*?)</a>', r'[\2](\1)', s)
    s = re.sub(r"</?b>", "**", s)
    s = re.sub(r"</?i>", "_", s)
    return _html.unescape(s)

PUSH_CONFIDENCE_MIN = 0.45  # below this => archive only

# Forward-looking schedule cues for the "오늘 주목할 이벤트" section. We only want
# items that point at something *upcoming*, not reports of past events, so the
# cues are deliberately future-oriented.
# Kept deliberately specific: ambiguous words like "마감"(시장 마감) or "회의"
# produce false positives on past news, so they are excluded.
_EVENT_CUES = (
    "예정", "앞두고", "예고", "개최", "열린다", "열려", "내일", "이번 주", "이번주",
    "다음 주", "다음주", "만기", "시한", "데드라인", "표결", "투표", "청문회",
    "fomc", "금통위", "연설",
)
_MAX_EVENTS = 5


def extract_events(articles: List["Article"]) -> List[str]:
    """Best-effort deterministic events: pick articles whose title signals an
    upcoming event. Used when the lead agent did not supply an events list."""
    out: List[str] = []
    seen = set()
    for a in articles:
        hay = a.title.lower()
        if any(cue in hay for cue in _EVENT_CUES):
            title = a.title.strip()
            if title and title not in seen:
                seen.add(title)
                out.append(title)
        if len(out) >= _MAX_EVENTS:
            break
    return out



def market_mood(economy: List[Article]) -> str:
    """One-line market mood from NewsData sentiment, when available."""
    sents = [a.sentiment for a in economy if a.sentiment in ("positive", "negative", "neutral")]
    if not sents:
        return "감성 데이터 없음 (제목·요약 기준 참고)"
    c = Counter(sents)
    pos, neg, neu = c.get("positive", 0), c.get("negative", 0), c.get("neutral", 0)
    total = pos + neg + neu
    if pos > neg and pos >= neu:
        tone = "위험 선호(낙관)"
    elif neg > pos and neg >= neu:
        tone = "위험 회피(신중)"
    else:
        tone = "혼조/중립"
    return f"{tone} — 긍정 {pos}·중립 {neu}·부정 {neg} (n={total})"


def _split_push_archive(arts: List[Article]) -> Tuple[List[Article], List[Article]]:
    push, archive = [], []
    for a in arts:
        weak_evidence = (not a.evidence) or a.evidence.endswith("(LLM 미사용)")
        if a.confidence < PUSH_CONFIDENCE_MIN or (weak_evidence and a.confidence < 0.6):
            archive.append(a)
        else:
            push.append(a)
    return push, archive


def _fmt_article(a: Article, idx: int) -> str:
    """One article as an HTML block: bold clickable title, then supporting lines
    with light visual markers so the message scans well on mobile."""
    title = _esc(a.title)
    if a.url:
        head = f'<b>{idx}. <a href="{_esc_attr(a.url)}">{title}</a></b>'
    else:
        head = f"<b>{idx}. {title}</b>"
    lines = [head]
    if a.one_liner:
        lines.append(_esc(a.one_liner))
    if a.why_it_matters:
        lines.append(f"💡 <i>{_esc(a.why_it_matters)}</i>")
    if a.implications:
        lines.append(f"▸ 함의: {_esc(a.implications)}")
    meta = []
    if a.tags:
        meta.append(" ".join("#" + _esc(t) for t in a.tags))
    if a.source_name:
        meta.append(f"📰 {_esc(a.source_name)}")
    if meta:
        lines.append("　" + " · ".join(meta))
    flags = [f for f in a.flags if f]
    if flags:
        lines.append(f"⚠️ {_esc(', '.join(flags))}")
    if a.related:
        lines.append(f"↳ 관련 보도 {len(a.related)}건")
    return "\n".join(lines)


def _section(header: str, arts: List[Article], *, source_failed: bool = False) -> List[str]:
    out = [f"<b>{header}</b>"]
    if not arts:
        out.append("· 데이터 수집 실패" if source_failed else "· 발송 기준(신뢰도) 미달 — 아카이브 참고")
        return out
    for i, a in enumerate(arts, 1):
        out.append(_fmt_article(a, i))
        out.append("")  # blank line between articles for breathing room
    if out and out[-1] == "":
        out.pop()
    return out


def _bullets(items, cap: int) -> List[str]:
    return [f"· {_esc(str(x).strip())}" for x in (items or []) if str(x).strip()][:cap]


def render_briefing(selected: List[Article], *, now: datetime | None = None,
                    failed_sources: List[str] | None = None,
                    events: List[str] | None = None,
                    top_insight: List[str] | None = None,
                    whats_changed: List[str] | None = None,
                    themes: List[str] | None = None) -> Tuple[str, str, dict]:
    """Return (telegram_text, markdown_archive, stats).

    telegram_text contains only push-worthy items; markdown_archive contains
    everything (push + archived) for the committed briefs/ file.
    """
    now = now or datetime.now(KST)
    date_str = now.astimezone(KST).strftime("%Y-%m-%d (%a) · %H:%M KST")
    failed_sources = failed_sources or []

    by_tag = {SOURCE_A: [], SOURCE_B: [], SOURCE_C: []}
    for a in selected:
        by_tag.get(a.source_tag, by_tag[SOURCE_A]).append(a)

    push_a, arch_a = _split_push_archive(by_tag[SOURCE_A])
    push_b, arch_b = _split_push_archive(by_tag[SOURCE_B])
    push_c, arch_c = _split_push_archive(by_tag[SOURCE_C])

    # --- Headline 3-line summary (from highest-confidence push items) ------
    head_pool = sorted(push_a + push_b + push_c, key=lambda x: x.confidence, reverse=True)[:3]
    headlines = [f"{_CIRCLED[i]} {_esc(a.one_liner or a.title)}" for i, a in enumerate(head_pool)]

    tg: List[str] = [f"📅 <b>오늘의 뉴스 브리핑</b>", _esc(date_str), ""]

    # --- Insight blocks (agent-generated) ---------------------------------
    insight_lines = _bullets(top_insight, 3)
    if insight_lines:
        tg.append("🔭 <b>오늘의 관전 포인트</b>")
        tg += insight_lines
        tg.append("")

    changed_lines = _bullets(whats_changed, 4)
    if changed_lines:
        tg.append("🔁 <b>어제 대비 달라진 점</b>")
        tg += changed_lines
        tg.append("")

    theme_lines = _bullets(themes, 3)
    if theme_lines:
        tg.append("🧭 <b>오늘의 핵심 테마</b>")
        tg += theme_lines
        tg.append("")

    tg.append("📌 <b>오늘의 헤드라인</b>")
    tg += headlines if headlines else ["· 핵심 헤드라인 없음"]
    tg.append("")

    tg += _section("💹 경제·시장", push_b, source_failed=SOURCE_B in failed_sources)
    tg.append("")
    tg.append(f"📊 <b>시장 분위기</b> · {_esc(market_mood(by_tag[SOURCE_B]))}")
    if SOURCE_B in failed_sources:
        tg.append("· (경제 소스 일부 실패 — 대체 소스 사용)")
    tg.append("")

    tg += _section("🌐 시사·국제", push_a, source_failed=SOURCE_A in failed_sources)
    tg.append("")

    tg += _section("🪙 크립토 (보조)", push_c, source_failed=SOURCE_C in failed_sources)
    tg.append("")

    # Events: prefer the lead agent's curated list; else derive deterministically.
    event_lines = [str(e).strip() for e in (events or []) if str(e).strip()]
    if not event_lines:
        event_lines = extract_events(selected)
    tg.append("🗓 <b>오늘 주목할 이벤트</b>")
    if event_lines:
        tg += [f"· {_esc(e)}" for e in event_lines[:_MAX_EVENTS]]
    else:
        tg.append("· 예정된 주요 일정이 식별되지 않았습니다.")

    if failed_sources:
        tg.append("")
        tg.append(f"ℹ️ 수집 실패 소스: {_esc(', '.join(failed_sources))}")

    telegram_text = "\n".join(tg).strip()

    # --- Markdown archive (everything) — convert the HTML back to Markdown --
    md = [f"# 뉴스 브리핑 — {date_str}", "", _html_to_md(telegram_text), ""]
    archived = arch_a + arch_b + arch_c
    if archived:
        md.append("\n---\n## 아카이브 (낮은 신뢰도/약한 근거 — 미발송)\n")
        for i, a in enumerate(archived, 1):
            md.append(_html_to_md(_fmt_article(a, i)))
            if a.evidence:
                md.append(f"　• 근거: {a.evidence}")
            md.append("")
    markdown = "\n".join(md).strip() + "\n"

    stats = {
        "selected": len(selected),
        "pushed": len(push_a) + len(push_b) + len(push_c),
        "archived": len(archived),
        "failed_sources": failed_sources,
    }
    return telegram_text, markdown, stats
