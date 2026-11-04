"""Single-call structured summarisation with evidence enforcement + translation.

지표 중심 설계(2026-11-04): 선별된 기사마다 ONE batched LLM call로
제목(번역) / 한줄요약 / indicators / confidence / flags 만 만든다. 예전 필드
(왜 중요한지·함의·태그·근거)는 선택 — 응답에 있으면 받고, 없으면 비워 둔다.

Hard rules pushed into the prompt:
- Reason ONLY from the provided title + summary snippet; if the source gives no
  body, do not infer beyond that snippet.
- Non-Korean articles: translated title on top, original title in parentheses
  below — "번역제목 (원문: Original Title)".
- Flag unsourced certainty words ("likely/clearly/will definitely") not
  attributable to the source.

Heuristic fallback (no LLM): 제목을 그대로 한 줄 요약으로 쓰고, 낮은 신뢰
표식("(LLM 미사용)" evidence)을 붙인다 — 분석을 지어내지 않는다.
"""

from __future__ import annotations

import json
from typing import List

from . import config, llm, relevance
from .model import Article

_SUM_SYSTEM = (
    "당신은 한국어 아침 뉴스 브리핑 편집자입니다. 핵심 지표나 보유 종목을 움직이는 "
    "정치·경제 뉴스만 다룹니다. 각 기사에 대해 제공된 제목과 요약 스니펫만 근거로 "
    "한 줄 요약을 쓰세요. 스니펫 범위를 넘는 추론은 금지합니다. 한국어가 아닌 기사는 "
    "제목을 한국어로 번역하세요(원문 보존은 파이썬이 처리). 'likely/clearly/will "
    "definitely'처럼 출처에 귀속되지 않는 단정 표현은 flags에 'unsourced-claim'으로 "
    "표시하세요. 사실과 의견을 구분하고 과장하지 마세요. 반드시 JSON만 출력하세요."
)


def _payload(articles: List[Article]) -> str:
    """Compact one-row-per-article JSON: empty fields and URLs are omitted
    (see Article.to_llm_dict) and snippets truncated — the model only ever
    reasons over title + snippet anyway."""
    rows = [json.dumps(a.to_llm_dict(i, snippet_chars=config.SNIPPET_CHARS),
                       ensure_ascii=False, separators=(",", ":"))
            for i, a in enumerate(articles)]
    return "[\n" + ",\n".join(rows) + "\n]"


def _llm_summarize(articles: List[Article]) -> bool:
    user = (
        f"{len(articles)}건의 기사를 한 줄씩 요약하세요. 각 행: id/title/source/snippet/"
        f"category/indicators(결정론 태그).\n\n{_payload(articles)}\n\n"
        f"indicators는 이 키 중에서만 고르세요: {', '.join(relevance.INDICATOR_KEYS)}.\n"
        '출력 형식: {"items": [{"id": 0, "title": "한국어 제목", "one_liner": "한 줄 요약", '
        '"indicators": ["ust10y"], "confidence": 0.0~1.0, "flags": []}]}'
    )
    # Output is 8 one-liners — keep generation tight.
    result = llm.complete_json(_SUM_SYSTEM, user, max_tokens=1500)
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
        apply_item(a, item)
    return True


def _text(v) -> str:
    return str(v).strip() if v is not None else ""


def apply_item(a: Article, item: dict) -> None:
    """선별·요약 항목 하나를 기사에 반영한다 — selection.json의 ``selected`` 행과
    자율 경로 LLM 응답의 ``items`` 행이 같은 모양이라 둘 다 이 함수를 쓴다.

    필수: title, one_liner, confidence, flags. ``indicators``가 없거나 유효 키가
    없으면 결정론 채점이 붙인 값을 유지한다. 구형 필드(why_it_matters·
    implications·tags·evidence)는 있으면 받고, 없으면 빈 값이다."""
    a.one_liner = _text(item.get("one_liner"))
    a.why_it_matters = _text(item.get("why_it_matters"))
    a.implications = _text(item.get("implications"))
    tags = item.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    a.tags = [str(t) for t in tags if str(t).strip()][:5]
    try:
        a.confidence = max(0.0, min(1.0, float(item.get("confidence", a.confidence))))
    except (ValueError, TypeError):
        pass
    a.evidence = _text(item.get("evidence"))
    flags = item.get("flags") or []
    if isinstance(flags, str):
        flags = [flags]
    for f in flags:
        if f and str(f) not in a.flags:
            a.flags.append(str(f))
    indicators = relevance.clean_indicators(item.get("indicators"))
    if indicators is not None:
        a.indicators = indicators
    _apply_translation(a, _text(item.get("title")))


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
    """No-LLM structuring: 제목을 그대로 한 줄 요약으로 쓴다. 스니펫 너머 추론 금지."""
    a.one_liner = a.title
    a.why_it_matters = ""  # cannot assert importance without analysis
    if not a.tags:
        a.tags = [a.category] if a.category else []
    a.evidence = "원문 제목 (LLM 미사용)"
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
