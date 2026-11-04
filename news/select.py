"""Single-call batch selection with diversity caps.

Per spec: do NOT score articles one-by-one. Send the whole candidate pool to the
LLM in ONE call and have it pick the final set. Enforce diversity caps:
  - at most MAX_PER_SOURCE (2) per outlet,
  - at most MAX_PER_CLUSTER (2) per topic cluster.

If the LLM is unavailable/fails, fall back to a deterministic heuristic that
respects the same caps: 관련도 점수(news/relevance.py) 순, 동점이면
confidence + recency.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import List

from . import config, llm
from .model import Article

_SELECT_SYSTEM = (
    "당신은 한국어 아침 뉴스 브리핑의 편집장입니다. 후보 기사 풀 전체를 한 번에 검토하여 "
    "핵심 지표(indicators)나 보유 종목을 움직이는 정치·경제 뉴스만 선별합니다. "
    "후보는 이미 관련도 점수순이며 무관한 기사는 제외되었습니다. "
    "추측·과장 없이, 지표 영향력·시의성·신뢰도를 기준으로 선택하세요. "
    "다양성 규칙: 한 매체당 최대 2건, 한 주제 클러스터당 최대 2건. "
    "반드시 JSON만 출력하세요."
)


def _candidate_payload(articles: List[Article]) -> str:
    """Compact, snippet-free rows (selection needs titles only). age_h is hours
    since publication — far fewer tokens than full ISO timestamps."""
    rows = [json.dumps(a.to_llm_dict(i, snippet_chars=None),
                       ensure_ascii=False, separators=(",", ":"))
            for i, a in enumerate(articles)]
    return "[\n" + ",\n".join(rows) + "\n]"


def _llm_select(articles: List[Article], max_items: int) -> List[int] | None:
    user = (
        f"후보 기사 풀(총 {len(articles)}건). 최대 {max_items}건을 선별하세요.\n"
        f"매체(source)당 최대 {config.MAX_PER_SOURCE}건.\n"
        f"category·indicators를 참고해 지표·보유 종목 영향이 큰 순으로 고르세요. "
        f"age_h는 발행 후 경과 시간(시간 단위).\n\n"
        f"{_candidate_payload(articles)}\n\n"
        '출력 형식: {"selected": [기사 id 목록(중요도 순)]}'
    )
    # Output is just an id list — cap generation tightly.
    result = llm.complete_json(_SELECT_SYSTEM, user, max_tokens=512)
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
    """관련도 점수 내림차순, 동점이면 confidence + recency. 채점 전 기사(score 0)만
    있으면 예전 순서(confidence + recency)와 같다."""
    scored = [(i, a.score, 0.6 * a.confidence + 0.4 * _recency_score(a))
              for i, a in enumerate(articles)]
    scored.sort(key=lambda t: (t[1], t[2]), reverse=True)
    return [i for i, _, _ in scored]


def _apply_caps(ordered_ids: List[int], articles: List[Article], max_items: int,
                *, max_per_source: int | None = None) -> List[Article]:
    """다양성 캡을 지키며 ``ordered_ids`` 순서대로 최대 ``max_items``건을 고른다.
    ``max_per_source``를 주지 않으면 최종 선별 캡(config.MAX_PER_SOURCE)."""
    source_cap = config.MAX_PER_SOURCE if max_per_source is None else max_per_source
    per_source: dict[str, int] = defaultdict(int)
    per_cluster: dict[object, int] = defaultdict(int)
    chosen: List[Article] = []
    for i in ordered_ids:
        a = articles[i]
        if per_source[a.source_name] >= source_cap:
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


def select(articles: List[Article], max_items: int = 8) -> tuple[List[Article], str]:
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
