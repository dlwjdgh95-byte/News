"""Multi-stage de-duplication with strong over-merge guards.

Order (per spec):
  1. Canonical URL normalisation  -> identical canonical URL = definite merge.
  2. Title token shingling        -> Jaccard >= 0.6 = same-event CANDIDATE.
  3. Over-merge guards (HIGHEST-PRIORITY SAFETY RULE):
       - time awareness: >=12h apart + a status-change cue  => keep separate.
       - entity/version distinction: different key entities or conflicting
         numbers => keep separate (and flag conflicting figures).

We MERGE only when: canonical URL identical, OR
(title Jaccard >= 0.6 AND publish times close AND key entities match).
When in doubt, DO NOT merge — keep articles separate.

Representative of a cluster = highest-confidence article; the rest are recorded
as "관련 보도" (related) on the representative.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from . import config
from .model import Article

# --------------------------------------------------------------------------
# 1. Canonical URL normalisation
# --------------------------------------------------------------------------
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = {
    "ref", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "spm",
    "cmpid", "cmp", "ito", "ns_campaign", "ns_mchannel", "ocid", "smid",
    "source", "ref_src", "ref_url", "_hsenc", "_hsmi", "yclid", "msclkid",
}


def canonical_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Drop tracking query params, keep meaningful ones.
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
         if not (k.lower() in _TRACKING_EXACT or k.lower().startswith(_TRACKING_PREFIXES))]
    q.sort()
    query = urlencode(q)
    path = parts.path.rstrip("/") or "/"
    # Fragment dropped entirely.
    return urlunsplit((scheme, netloc, path, query, ""))


# --------------------------------------------------------------------------
# 2. Title tokenisation / shingling
# --------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[0-9a-z가-힣]+")
_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "as", "at", "by", "with", "from", "after", "over", "단독", "속보", "종합",
}


def _tokens(title: str) -> set[str]:
    toks = [t for t in _TOKEN_RE.findall(title.lower()) if t not in _STOP and len(t) > 1]
    return set(toks)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# --------------------------------------------------------------------------
# Key entities + status-change cues
# --------------------------------------------------------------------------
_NUMBER_RE = re.compile(r"\d[\d,\.]*\s*(?:조|억|만|원|달러|%|％|bn|m|billion|million|trillion)?", re.I)
# Capitalised runs (latin proper nouns) + version-ish tokens.
_PROPER_RE = re.compile(r"\b[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*\b")
_VERSION_RE = re.compile(r"\b(?:v?\d+(?:\.\d+)+|pro|max|ultra|mini|flash|plus|lite)\b", re.I)

_STATUS_CUES = [
    "첫날", "이후", "후속", "급락", "급등", "철회", "사과", "번져", "확산", "추가",
    "재차", "결국", "마침내", "번복", "취소", "연기", "재개", "속보", "2차", "3차",
    "follow", "fallout", "after", "plunge", "surge", "retract", "apolog",
    "spread", "escalat", "second", "third", "update", "reverses", "u-turn",
]


# Ultra-common Korean news tokens that are NOT distinguishing entities. Kept
# small on purpose — the symmetric-difference guard already cancels shared
# tokens, so this only needs to suppress trivially-common standalone words.
_KO_STOP = {
    "속보", "단독", "종합", "영상", "포토", "사진", "기자", "오늘", "내일",
    "관련", "예정", "전망", "발표", "공개", "결정",
}
_HANGUL_TOKEN_RE = re.compile(r"[가-힣]{2,}")


def _korean_entities(title: str) -> set[str]:
    """Distinguishing Korean tokens (length>=2 Hangul, minus stopwords).

    Latin proper-noun matching ([A-Z]...) never fires on Korean, which left the
    over-merge entity guard inert for the dominant language. These tokens feed a
    symmetric-difference check so two Korean stories that share a template but
    differ in their key subject (e.g. 코스피 vs 코스닥) stay separate.
    """
    return {t for t in _HANGUL_TOKEN_RE.findall(title)
            if t not in _STOP and t not in _KO_STOP}


def extract_key_entities(title: str) -> List[str]:
    ents = set()
    for m in _PROPER_RE.findall(title):
        ents.add(m.strip())
    for m in _VERSION_RE.findall(title):
        ents.add(m.lower())
    for m in _NUMBER_RE.findall(title):
        norm = m.replace(" ", "").lower()
        if any(ch.isdigit() for ch in norm):
            ents.add(norm)
    # Korean distinguishing tokens (proper-noun-ish subjects).
    ents.update(_korean_entities(title))
    return sorted(ents)


def _numbers(entities: List[str]) -> set[str]:
    return {e for e in entities if any(c.isdigit() for c in e)}


# A magnitude unit (조/억/원/%/달러…) makes a number "meaningful" for conflict
# detection. Bare integers like a quarter ("2분기" -> "2") or a year must NOT
# count, or an incidental shared "2" would mask a real 10조-vs-12조 conflict.
_UNIT_RE = re.compile(r"(조|억|만|원|달러|%|％|bn|m|billion|million|trillion)$", re.I)


def _unit_numbers(entities: List[str]) -> set[str]:
    return {e for e in _numbers(entities) if _UNIT_RE.search(e)}


_VERSION_WORDS = {"pro", "max", "ultra", "mini", "flash", "plus", "lite"}


def _versions(entities: List[str]) -> set[str]:
    out = set()
    for e in entities:
        if e in _VERSION_WORDS or _VERSION_RE.fullmatch(e):
            out.add(e)
    return out


def _has_status_cue(title: str) -> bool:
    low = title.lower()
    return any(cue in low for cue in _STATUS_CUES)


# --------------------------------------------------------------------------
# Merge decision
# --------------------------------------------------------------------------
def _time_gap_hours(a: Article, b: Article) -> Optional[float]:
    if a.published_at is None or b.published_at is None:
        return None
    return abs((a.published_at - b.published_at).total_seconds()) / 3600.0


def can_merge(a: Article, b: Article) -> tuple[bool, list[str]]:
    """Return (should_merge, flags). Conservative: default to NOT merging."""
    flags: list[str] = []

    # Rule 0: identical canonical URL => definite merge.
    if a.canonical_url and a.canonical_url == b.canonical_url:
        return True, flags

    sim = jaccard(_tokens(a.title), _tokens(b.title))
    if sim < config.JACCARD_TITLE_THRESHOLD:
        return False, flags

    # --- candidate same-event; now apply over-merge guards ---------------
    gap = _time_gap_hours(a, b)

    # Time awareness: far apart + a status-change cue => follow-up, keep apart.
    if gap is not None and gap >= config.TIME_SPLIT_HOURS:
        if _has_status_cue(a.title) or _has_status_cue(b.title):
            return False, flags

    # Entity/version distinction.
    ents_a = a.key_entities or extract_key_entities(a.title)
    ents_b = b.key_entities or extract_key_entities(b.title)

    # Conflicting key figures => keep both, flag possible misinformation. Only
    # unit-bearing figures count, so an incidental shared bare number (a quarter
    # or year) can't mask a real magnitude conflict (e.g. 10조 vs 12조).
    nums_a, nums_b = _unit_numbers(ents_a), _unit_numbers(ents_b)
    if nums_a and nums_b and not (nums_a & nums_b):
        return False, ["conflicting-figures"]

    # Version/variant distinction: e.g. Gemini Pro != Gemini Flash. If both
    # titles carry a version word and they differ, they are different subjects.
    vers_a, vers_b = _versions(ents_a), _versions(ents_b)
    if vers_a and vers_b and vers_a != vers_b:
        return False, flags

    # Non-numeric proper-noun entities differ entirely => different subject.
    names_a = {e for e in ents_a if not any(c.isdigit() for c in e)}
    names_b = {e for e in ents_b if not any(c.isdigit() for c in e)}
    if names_a and names_b and not (names_a & names_b):
        return False, flags

    # Korean distinguishing-token guard: if each title carries a unique
    # Korean subject token the other lacks (e.g. 코스피 vs 코스닥), they concern
    # different subjects despite the high title overlap -> keep separate.
    ko_a = _korean_entities(a.original_title or a.title)
    ko_b = _korean_entities(b.original_title or b.title)
    if (ko_a - ko_b) and (ko_b - ko_a):
        return False, flags

    # Required merge condition per spec: title similarity AND CLOSE publish time
    # (발행 시각 근접). If we cannot confirm closeness, do NOT merge — when in
    # doubt, keep separate.
    if gap is None or gap < config.TIME_SPLIT_HOURS:
        return True, flags
    return False, flags


# --------------------------------------------------------------------------
# Clustering
# --------------------------------------------------------------------------
def deduplicate(articles: List[Article]) -> List[Article]:
    """Return cluster representatives, each carrying related[] + flags."""
    for a in articles:
        a.canonical_url = canonical_url(a.url)
        if not a.key_entities:
            a.key_entities = extract_key_entities(a.original_title or a.title)

    reps: List[Article] = []
    for art in articles:
        placed = False
        for rep in reps:
            merge, flags = can_merge(rep, art)
            if merge:
                for f in flags:
                    if f not in rep.flags:
                        rep.flags.append(f)
                # Keep the higher-confidence article as representative.
                if art.confidence > rep.confidence:
                    # New rep inherits the old rep's related[] plus the old rep
                    # itself, never referencing itself.
                    merged_related = [rep.url] + rep.related + art.related
                    art.related = [u for u in dict.fromkeys(merged_related) if u != art.url]
                    art.flags = list(dict.fromkeys(art.flags + rep.flags))
                    art.cluster_id = rep.cluster_id
                    reps[reps.index(rep)] = art
                else:
                    rep.related.append(art.url)
                placed = True
                break
            else:
                # If guards flagged conflicting figures across a near-dup, record it.
                for f in flags:
                    if f not in art.flags:
                        art.flags.append(f)
        if not placed:
            art.cluster_id = len(reps)
            reps.append(art)
    return reps
