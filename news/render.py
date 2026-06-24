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

from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

from .model import Article, SOURCE_A, SOURCE_B, SOURCE_C

KST = timezone(timedelta(hours=9))

PUSH_CONFIDENCE_MIN = 0.45  # below this => archive only


def market_mood(economy: List[Article]) -> str:
    """One-line market mood from NewsData sentiment, when available."""
    sents = [a.sentiment for a in economy if a.sentiment in ("positive", "negative", "neutral")]
    if not sents:
        return "시장 분위기: 감성 데이터 없음 (제목·요약 기준 참고)"
    c = Counter(sents)
    pos, neg, neu = c.get("positive", 0), c.get("negative", 0), c.get("neutral", 0)
    total = pos + neg + neu
    if pos > neg and pos >= neu:
        tone = "위험 선호(낙관)"
    elif neg > pos and neg >= neu:
        tone = "위험 회피(신중)"
    else:
        tone = "혼조/중립"
    return f"시장 분위기: {tone} — 긍정 {pos}·중립 {neu}·부정 {neg} (n={total})"


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
    lines = [f"{idx}. {a.title}"]
    if a.one_liner:
        lines.append(f"   • 한줄요약: {a.one_liner}")
    if a.why_it_matters:
        lines.append(f"   • 왜 중요한지: {a.why_it_matters}")
    if a.tags:
        lines.append(f"   • 태그: {', '.join('#' + t for t in a.tags)}")
    flags = [f for f in a.flags if f]
    if flags:
        lines.append(f"   ⚠️ 주의: {', '.join(flags)}")
    lines.append(f"   🔗 {a.url}")
    if a.related:
        lines.append(f"   ↳ 관련 보도 {len(a.related)}건")
    return "\n".join(lines)


def _section(title: str, arts: List[Article], *, source_failed: bool = False) -> List[str]:
    out = [title]
    if not arts:
        out.append("· 데이터 수집 실패" if source_failed else "· 발송 기준(신뢰도) 미달 — 아카이브 참고")
        return out
    for i, a in enumerate(arts, 1):
        out.append(_fmt_article(a, i))
    return out


def render_briefing(selected: List[Article], *, now: datetime | None = None,
                    failed_sources: List[str] | None = None) -> Tuple[str, str, dict]:
    """Return (telegram_text, markdown_archive, stats).

    telegram_text contains only push-worthy items; markdown_archive contains
    everything (push + archived) for the committed briefs/ file.
    """
    now = now or datetime.now(KST)
    date_str = now.astimezone(KST).strftime("%Y-%m-%d (%a) 07:30 KST")
    failed_sources = failed_sources or []

    by_tag = {SOURCE_A: [], SOURCE_B: [], SOURCE_C: []}
    for a in selected:
        by_tag.get(a.source_tag, by_tag[SOURCE_A]).append(a)

    push_a, arch_a = _split_push_archive(by_tag[SOURCE_A])
    push_b, arch_b = _split_push_archive(by_tag[SOURCE_B])
    push_c, arch_c = _split_push_archive(by_tag[SOURCE_C])

    # --- Headline 3-line summary (from highest-confidence push items) ------
    head_pool = sorted(push_a + push_b + push_c, key=lambda x: x.confidence, reverse=True)[:3]
    headlines = [f"{i}. {a.one_liner or a.title}" for i, a in enumerate(head_pool, 1)]

    tg: List[str] = [f"📅 오늘의 뉴스 브리핑 — {date_str}", ""]
    tg.append("📌 오늘의 헤드라인")
    tg += headlines if headlines else ["· 핵심 헤드라인 없음"]
    tg.append("")

    tg += _section("💹 경제·시장", push_b, source_failed=SOURCE_B in failed_sources)
    tg.append(market_mood(by_tag[SOURCE_B]))
    if SOURCE_B in failed_sources:
        tg.append("· (경제 소스 일부 실패 — 대체 소스 사용)")
    tg.append("")

    tg += _section("🌐 시사·국제", push_a, source_failed=SOURCE_A in failed_sources)
    tg.append("")

    tg += _section("🪙 크립토 (보조)", push_c, source_failed=SOURCE_C in failed_sources)
    tg.append("")

    tg.append("🗓 오늘 주목할 이벤트")
    tg.append("· 일정 데이터 소스 미연동 — 추후 보강 예정")

    if failed_sources:
        tg.append("")
        tg.append(f"ℹ️ 수집 실패 소스: {', '.join(failed_sources)}")

    telegram_text = "\n".join(tg).strip()

    # --- Markdown archive (everything) ------------------------------------
    md = [f"# 뉴스 브리핑 — {date_str}", "", telegram_text, ""]
    archived = arch_a + arch_b + arch_c
    if archived:
        md.append("\n---\n## 아카이브 (낮은 신뢰도/약한 근거 — 미발송)\n")
        for i, a in enumerate(archived, 1):
            md.append(_fmt_article(a, i))
            if a.evidence:
                md.append(f"   • 근거: {a.evidence}")
            md.append("")
    markdown = "\n".join(md).strip() + "\n"

    stats = {
        "selected": len(selected),
        "pushed": len(push_a) + len(push_b) + len(push_c),
        "archived": len(archived),
        "failed_sources": failed_sources,
    }
    return telegram_text, markdown, stats
