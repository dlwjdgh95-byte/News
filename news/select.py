"""Single-call batch selection with diversity caps.

Per spec: do NOT score articles one-by-one. Send the whole candidate pool to the
LLM in ONE call and have it pick the final set. Enforce diversity caps:
  - at most MAX_PER_SOURCE (2) per outlet,
  - at most MAX_PER_CLUSTER (2) per topic cluster.

If the LLM is unavailable/fails, fall back to a deterministic heuristic
(confidence + recency) that respects the same caps.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import List

from . import config, llm
from .model import Article

_SELECT_SYSTEM = (
    "당신은 한국어 아침 뉴스 브리핑의 편집장입니다. 후보 기사 풀 전체를 한 번에 검토하여 "
    "가장 중요하고 신뢰도 높은 기사들만 선별합니다. 연예·스포츠는 이미 제외되었습니다. "
    "추측·과장 없이, 영향력·시의성·신뢰도를 기준으로 선택하세요. "
    "다양성 규칙: 한 매체당 최대 2건, 한 주제 클러스터당 최대 2건. "
    "반드시 JSON만 출력하세요."
)


def _candidate_payload(articles: List[Article]) -> str:
    lines = []
    for i, a in enumerate(articles):
        pub = a.published_at.isoformat() if a.published_at else "?"
        lines.append(
            f'{{"id": {i}, "source_tag": "{a.source_tag}", "source": "{a.source_name}", '
            f'"title": {a.title!r}, "category": "{a.category}", "sentiment": "{a.sentiment}", '
            f'"published": "{pub}", "confidence": {a.confidence:.2f}}}'
        )
    return "[\n" + ",\n".join(lines) + "\n]"


def _llm_select(articles: List[Article], max_items: int) -> List[int] | None:
    user = (
        f"후보 기사 풀(총 {len(articles)}건). 최대 {max_items}건을 선별하세요.\n"
        f"매체당 최대 {config.MAX_PER_SOURCE}건, 주제 클러스터당 최대 {config.MAX_PER_CLUSTER}건.\n"
        f"경제·시사·국제·크립토의 균형을 맞추세요.\n\n"
        f"{_candidate_payload(articles)}\n\n"
        '출력 형식: {"selected": [기사 id 목록(중요도 순)]}'
    )
    result = llm.complete_json(_SELECT_SYSTEM, user)
    if not result or "selected" not in result:
        return None
    ids = []
    for x in result["selected"]:
        try:
            i = int(x)
        except (ValueError, TypeError):
            continue
        if 0 <= i < len(articles):
            ids.append(i)
    return ids or None


def _recency_score(a: Article) -> float:
    if a.published_at is None:
        return 0.0
    age_h = (datetime.now(timezone.utc) - a.published_at).total_seconds() / 3600.0
    return max(0.0, 1.0 - age_h / 48.0)  # decays over 2 days


def _heuristic_order(articles: List[Article]) -> List[int]:
    scored = [(i, 0.6 * a.confidence + 0.4 * _recency_score(a)) for i, a in enumerate(articles)]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [i for i, _ in scored]


def _apply_caps(ordered_ids: List[int], articles: List[Article], max_items: int) -> List[Article]:
    per_source: dict[str, int] = defaultdict(int)
    per_cluster: dict[object, int] = defaultdict(int)
    chosen: List[Article] = []
    for i in ordered_ids:
        a = articles[i]
        if per_source[a.source_name] >= config.MAX_PER_SOURCE:
            continue
        cid = a.cluster_id if a.cluster_id is not None else f"u{i}"
        if per_cluster[cid] >= config.MAX_PER_CLUSTER:
            continue
        chosen.append(a)
        per_source[a.source_name] += 1
        per_cluster[cid] += 1
        if len(chosen) >= max_items:
            break
    return chosen


def select(articles: List[Article], max_items: int = 14) -> tuple[List[Article], str]:
    """Return (selected_articles, method) where method is 'llm' or 'heuristic'."""
    if not articles:
        return [], "heuristic"
    method = "heuristic"
    order = _llm_select(articles, max_items)
    if order is not None:
        method = "llm"
        # Append any high-confidence items the LLM omitted, so caps can still fill.
        seen = set(order)
        order = order + [i for i in _heuristic_order(articles) if i not in seen]
    else:
        order = _heuristic_order(articles)
    chosen = _apply_caps(order, articles, max_items)
    return chosen, method
