"""Single-call structured summarisation with evidence enforcement + translation.

Per spec, for the selected articles we make ONE batched LLM call producing, per
article: 제목 / 한줄요약 / 왜 중요한지 / 태그 / confidence / evidence.

Hard rules pushed into the prompt:
- Reason ONLY from the provided title + summary snippet; if the source gives no
  body, do not infer beyond that snippet.
- Non-Korean articles: translated title on top, original title in parentheses
  below — "번역제목 (원문: Original Title)".
- Flag unsourced certainty words ("likely/clearly/will definitely") not
  attributable to the source.

Heuristic fallback (no LLM): use the source snippet verbatim as the one-liner,
keep the original title, attach a low confidence and an "no-llm" evidence note —
never inventing analysis.
"""

from __future__ import annotations

import json
from typing import List

from . import llm
from .model import Article

_SUM_SYSTEM = (
    "당신은 한국어 뉴스 브리핑 편집자입니다. 각 기사에 대해 제공된 제목과 요약 스니펫만 "
    "근거로 사용하세요. 스니펫 범위를 넘는 추론은 금지합니다. 본문이 없으면 제목+요약 "
    "한도 내에서만 작성하세요. 한국어가 아닌 기사는 제목을 한국어로 번역하되 원문 제목을 "
    "보존하세요. 'likely/clearly/will definitely'처럼 출처에 귀속되지 않는 단정 표현은 "
    "flags에 'unsourced-claim'으로 표시하세요. 사실과 의견을 구분하고 과장하지 마세요. "
    "반드시 JSON만 출력하세요."
)


def _payload(articles: List[Article]) -> str:
    rows = []
    for i, a in enumerate(articles):
        rows.append({
            "id": i,
            "source_tag": a.source_tag,
            "source": a.source_name,
            "language": a.language,
            "original_title": a.original_title or a.title,
            "snippet": a.summary[:600],
            "category": a.category,
            "sentiment": a.sentiment,
            "confidence": round(a.confidence, 2),
            "existing_flags": a.flags,
        })
    return json.dumps(rows, ensure_ascii=False, indent=1)


def _llm_summarize(articles: List[Article]) -> bool:
    user = (
        f"{len(articles)}건의 기사를 구조화하세요.\n\n{_payload(articles)}\n\n"
        '각 기사에 대해 다음 JSON을 출력하세요:\n'
        '{"items": [{"id": 0, "title": "한국어 제목(번역 시 원문 보존은 original_title 사용)", '
        '"one_liner": "한줄요약", "why_it_matters": "왜 중요한지", "tags": ["태그"], '
        '"confidence": 0.0~1.0, "evidence": "인용한 원문 구절 + 출처", '
        '"flags": ["unsourced-claim 등"]}]}'
    )
    result = llm.complete_json(_SUM_SYSTEM, user, max_tokens=4096)
    if not result or "items" not in result:
        return False
    by_id = {}
    for item in result["items"]:
        try:
            by_id[int(item["id"])] = item
        except (KeyError, ValueError, TypeError):
            continue
    if not by_id:
        return False
    for i, a in enumerate(articles):
        item = by_id.get(i)
        if not item:
            _heuristic_one(a)
            continue
        translated = (item.get("title") or "").strip()
        a.one_liner = (item.get("one_liner") or "").strip()
        a.why_it_matters = (item.get("why_it_matters") or "").strip()
        tags = item.get("tags") or []
        a.tags = [str(t) for t in tags][:5]
        try:
            a.confidence = max(0.0, min(1.0, float(item.get("confidence", a.confidence))))
        except (ValueError, TypeError):
            pass
        a.evidence = (item.get("evidence") or "").strip()
        for f in (item.get("flags") or []):
            if f and f not in a.flags:
                a.flags.append(str(f))
        _apply_translation(a, translated)
    return True


def _apply_translation(a: Article, translated_title: str) -> None:
    """Non-Korean -> show translated title with original in parentheses."""
    if a.language and a.language != "ko" and translated_title:
        orig = a.original_title or a.title
        if translated_title.strip() != orig.strip():
            a.title = f"{translated_title} (원문: {orig})"
        else:
            a.title = translated_title
    elif translated_title:
        a.title = translated_title


def _heuristic_one(a: Article) -> None:
    """No-LLM structuring: never infer beyond the provided snippet."""
    snippet = a.summary.strip()
    a.one_liner = snippet[:160] if snippet else a.title
    a.why_it_matters = ""  # cannot assert importance without analysis
    if not a.tags:
        a.tags = [a.category] if a.category else []
    a.evidence = "원문 스니펫 (LLM 미사용)"
    if a.language and a.language != "ko":
        # No translation available offline: keep original, mark it.
        if "원문:" not in a.title:
            a.title = f"{a.original_title} (원문 미번역)"


def summarize(articles: List[Article]) -> str:
    """Fill structured fields on each article. Returns 'llm' or 'heuristic'."""
    if not articles:
        return "heuristic"
    if llm.available() and _llm_summarize(articles):
        return "llm"
    for a in articles:
        _heuristic_one(a)
    return "heuristic"
