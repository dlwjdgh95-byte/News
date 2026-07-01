"""Common article model — the cross-module contract.

Every collector (sources A/B/C) MUST produce a list of ``Article`` objects with
this exact shape. Every downstream stage (normalize, prefilter, dedup, select,
summarize, render) consumes and/or enriches these same objects. Keeping this
contract stable is what lets the collectors be developed independently and in
parallel.

Design rules:
- Collectors fill the *raw* fields they can observe from their source. They must
  NOT do dedup, filtering, summarization or translation — that is downstream.
- Fields that are computed later default to empty/None so a half-filled Article
  is still valid and serialisable at any pipeline stage.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


# Source tags per the spec:
#   A = 시사·정치·국제·한국 (Google News RSS + Guardian)
#   B = 경제·증시 (NewsData.io, fallback Guardian / Google News RSS business)
#   C = 크립토·핀테크 (crypto RSS feeds)
SOURCE_A = "A"
SOURCE_B = "B"
SOURCE_C = "C"
VALID_SOURCE_TAGS = {SOURCE_A, SOURCE_B, SOURCE_C}


@dataclass
class Article:
    """A single news item flowing through the pipeline.

    The first block of fields is what collectors are responsible for. The second
    block is enriched by later stages and starts empty.
    """

    # --- Collector-owned (raw) fields -------------------------------------
    title: str                                  # 제목 (display title; may be translated later)
    url: str                                    # 출처링크 (original article link)
    source_name: str                            # 매체명 (e.g. "Reuters", "한겨레")
    source_tag: str                             # 소스태그 A/B/C
    summary: str = ""                           # 요약 (RSS/API provided snippet, NOT LLM)
    published_at: Optional[datetime] = None     # 발행시각 (tz-aware UTC preferred)
    category: str = ""                           # 카테고리 (e.g. "business", "politics")
    sentiment: Optional[str] = None             # 감성점수: positive/neutral/negative (source B)
    language: str = ""                           # ISO-639-1 e.g. "en", "ko"
    original_title: str = ""                     # 원문제목 (kept distinct from translated title)

    # --- Downstream-enriched fields ---------------------------------------
    canonical_url: str = ""                      # set by dedup (normalised URL)
    confidence: float = 0.5                      # 신뢰도 0..1 (source reliability * evidence)
    key_entities: List[str] = field(default_factory=list)  # 핵심개체 (proper nouns/numbers)
    cluster_id: Optional[int] = None             # dedup cluster membership
    related: List[str] = field(default_factory=list)       # "관련 보도" urls merged into this rep

    # Structured summary fields (filled by the summarize stage)
    one_liner: str = ""                          # 한줄요약
    why_it_matters: str = ""                     # 왜 중요한지
    implications: str = ""                        # 2차 영향·함의 (누구에게/무엇을 지켜볼지)
    tags: List[str] = field(default_factory=list)
    evidence: str = ""                           # 근거: 인용 구절 + 출처
    flags: List[str] = field(default_factory=list)  # e.g. ["unsourced-claim", "conflicting-figures"]

    def __post_init__(self) -> None:
        if self.source_tag not in VALID_SOURCE_TAGS:
            raise ValueError(
                f"source_tag must be one of {sorted(VALID_SOURCE_TAGS)}, got {self.source_tag!r}"
            )
        # Preserve the original title for the translation stage if not given.
        if not self.original_title:
            self.original_title = self.title
        # Normalise published_at to tz-aware UTC when possible.
        if self.published_at is not None and self.published_at.tzinfo is None:
            self.published_at = self.published_at.replace(tzinfo=timezone.utc)

    # --- (de)serialisation helpers ---------------------------------------
    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        if self.published_at is not None:
            d["published_at"] = self.published_at.astimezone(timezone.utc).isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Article":
        d = dict(d)
        pa = d.get("published_at")
        if isinstance(pa, str) and pa:
            try:
                d["published_at"] = datetime.fromisoformat(pa)
            except ValueError:
                d["published_at"] = None
        # Drop unknown keys defensively so the model can evolve.
        known = {f.name for f in dataclasses.fields(cls)}
        d = {k: v for k, v in d.items() if k in known}
        return cls(**d)
