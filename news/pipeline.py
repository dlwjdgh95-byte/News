"""Orchestration.

Two ways to run the intelligence (selection + summary) step:

  * Autonomous (run_pipeline): collect -> normalize -> prefilter -> dedup ->
    relevance (지표·보유 키워드 채점, 무관 기사 제거) -> drop-sent -> pool cap
    (점수순 20건) -> select (1 LLM call + caps) -> summarize (1 LLM call) ->
    render -> archive. Used by the GitHub Actions path.

  * Hybrid (run_prepare + run_finalize): `run_prepare` does everything up to the
    candidate pool and writes state/candidates.json; a lead agent (subscription
    model) writes state/selection.json with the single-pass selection+summary;
    `run_finalize` renders and archives. See ORCHESTRATION.md.

Any failure / timeout / empty collection switches to the deterministic fallback
so a minimal briefing always exists for the day. A *quiet* day — collection
worked but no article touches an indicator or holding — is not a failure: it
produces a normal (non-degraded) brief with 0 items and ``quiet: true``.

Output is two files per day in briefs/:
  · <date>.md   — markdown archive, read by invest-wiki's collect_news.py
  · <date>.json — page IR, copied into Miner as briefing/data/news/<date>.json
                  by the integrated routine and rendered as the report page's
                  first tab

There is no Telegram delivery here any more (2026-08-15). The old double-send
guard went with it: nothing is delivered, so a partial delivery can no longer
race the fallback. Failure *alerts* still go to Telegram, but they live in
Miner's invest-wiki/scripts/notify.py, not in this repo.
"""

from __future__ import annotations

import json
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from . import (config, state, normalize, prefilter, dedup, relevance, select,
               summarize, render)
from .model import Article, SOURCE_A, SOURCE_B, SOURCE_C

KST = timezone(timedelta(hours=9))
COLLECT_TIMEOUT = 90  # seconds per source
# Agent-facing candidate view (slim, no URLs) vs full machine state. The lead
# agent reads candidates.json into its context, so that file is kept lean;
# pool.json round-trips the complete Articles for --finalize.
CANDIDATES_PATH = config.STATE_DIR / "candidates.json"
POOL_PATH = config.STATE_DIR / "pool.json"
SELECTION_PATH = config.STATE_DIR / "selection.json"
MAX_ITEMS = config.MAX_ITEMS  # 최종 기사 수 (기본 8, env NEWS_MAX_ITEMS)


def _collect_all() -> Tuple[List[Article], List[str]]:
    """Run the three read-only collectors in parallel; isolate failures."""
    from .collectors import source_a, source_b, source_c
    jobs = {SOURCE_A: source_a.collect, SOURCE_B: source_b.collect, SOURCE_C: source_c.collect}
    results: List[Article] = []
    failed: List[str] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {tag: ex.submit(fn) for tag, fn in jobs.items()}
        for tag, fut in futures.items():
            try:
                items = fut.result(timeout=COLLECT_TIMEOUT) or []
                if items:
                    results.extend(items)
                else:
                    failed.append(tag)
            except (FTimeout, Exception):  # noqa: BLE001 - isolate each source
                failed.append(tag)
    return results, failed


def _build_candidates() -> Tuple[List[Article], List[str], int]:
    """Collect -> normalize -> prefilter -> dedup -> relevance -> drop already-sent
    -> pool cap. Returns (pool, failed_sources, irrelevant_dropped).

    Only a genuinely empty collection raises (that is what the fallback is for).
    An empty pool after the relevance filter is a *quiet day*, not a broken one:
    it returns ``[]`` and the callers write a 0-item, non-degraded brief
    (``quiet: true``) instead of falling back to the irrelevant headlines we just
    filtered out. Each transform is guarded so one bad article can't crash the
    run, and if the sent-log filter removes everything we keep the best relevant
    items rather than forcing a fallback — a slightly-repeated item beats an
    ugly fallback."""
    config.ensure_dirs()
    raw, failed = _collect_all()
    if not raw:
        raise RuntimeError(f"collection produced no articles (failed={failed})")
    try:
        arts = normalize.normalize(raw)
        arts, _dropped = prefilter.prefilter(arts)
        reps = dedup.deduplicate(arts)
    except Exception as exc:  # noqa: BLE001 - never fall back over a bad title
        print(f"[pipeline] hygiene stage error ({exc}); using raw normalized items")
        reps = normalize.normalize(raw)
    # 지표 중심 필터: 핵심 지표·보유 종목 키워드에 안 걸리는 기사는 LLM 앞에서 버린다.
    irrelevant = 0
    try:
        reps, irrelevant = relevance.filter_relevant(reps)
    except Exception as exc:  # noqa: BLE001 - a scoring bug must not kill the day
        print(f"[pipeline] relevance stage error ({exc}); passing unscored items")
    if not reps:
        return [], failed, irrelevant  # 조용한 날 — 폴백이 아니다
    log = state.load_sent_log()
    fresh = [a for a in reps if not state.already_sent(log, a.canonical_url)]
    if not fresh:
        # Everything was sent before — don't fall back; keep the best few.
        fresh = _cap_pool(reps, MAX_ITEMS)
    return _cap_pool(fresh, config.MAX_CANDIDATES), failed, irrelevant


def _cap_pool(arts: List[Article], cap: int) -> List[Article]:
    """Bound the candidate pool that reaches the LLM/agent: 관련도 점수순(동점이면
    confidence + recency)으로 줄 세운 뒤 다양성 캡(매체당 POOL_MAX_PER_SOURCE=3,
    클러스터당 MAX_PER_CLUSTER)을 적용해 상위 ``cap``건만 남긴다. 풀의 매체당 캡은
    최종 캡(2)보다 하나 넉넉하다 — 점수가 2.0에서 자주 동점이라, 같으면 한 매체의
    차순위 기사가 풀에서 먼저 잘려 에이전트가 고를 여지가 준다.

    (2026-11-04 이전에는 소스 태그 A/B/C 라운드로빈으로 균형을 맞췄다. 지표 중심
    설계에서는 태그 균형보다 점수가 우선이다.)"""
    return select._apply_caps(select._heuristic_order(arts), arts, cap,
                              max_per_source=config.POOL_MAX_PER_SOURCE)


def _deliver(ir: dict, markdown: str, selected: List[Article], *, persist: bool):
    """Archive the briefing as markdown (for invest-wiki ingest) and as page IR
    (for the Miner report page). Returns (brief_path, ir_path).

    There is no delivery step to fail any more, so this no longer raises on a
    transport problem — only a genuinely unwritable disk gets here, and that
    should surface, not silently fall back to a second briefing.

    ``persist=False`` (--dry-run) writes nothing at all. The archive write used
    to happen even under --dry-run, which left the working tree dirty after
    every dry run; with the IR file added that would now be two stray files."""
    now = datetime.now(KST)
    date_str = now.strftime("%Y-%m-%d")
    brief_path = config.BRIEFS_DIR / f"{date_str}.md"
    ir_path = config.BRIEFS_DIR / f"{date_str}.json"

    if not persist:
        return "(dry-run)", "(dry-run)"

    brief_path.write_text(markdown, encoding="utf-8")
    ir_path.write_text(json.dumps(ir, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")

    # sent_log is misnamed for what it now does: it stops *re-selection*, not
    # re-sending. Yesterday's articles must not be picked again today, so it is
    # recorded on every persisted run — the old `send and persist` gate would
    # now never fire and every day would re-surface the same stories.
    log = state.load_sent_log()
    pushed_urls = [a.canonical_url for a in selected
                   if a.confidence >= render.PUSH_CONFIDENCE_MIN]
    state.record_sent(log, pushed_urls, now)
    state.prune_sent_log(log)
    state.save_sent_log(log)
    return str(brief_path), str(ir_path)


# --------------------------------------------------------------------------
# Autonomous path
# --------------------------------------------------------------------------
def run_pipeline(persist: bool = True) -> dict:
    """Execute the full autonomous pipeline. Raises on unrecoverable failure so
    run() can switch to the fallback path."""
    fresh, failed, irrelevant = _build_candidates()
    quiet = not fresh
    if quiet:
        selected, sel_method, sum_method = [], "quiet", "quiet"
    else:
        selected, sel_method = select.select(fresh, max_items=MAX_ITEMS)
        sum_method = summarize.summarize(selected)
    _tg, markdown, stats = render.render_briefing(selected, failed_sources=failed, quiet=quiet)
    ir = render.render_ir(selected, failed_sources=failed, quiet=quiet)
    brief_path, ir_path = _deliver(ir, markdown, selected, persist=persist)
    out = {
        "mode": "main",
        "selection": sel_method,
        "summary": sum_method,
        "failed_sources": failed,
        "irrelevant_dropped": irrelevant,
        "brief_path": brief_path,
        "ir_path": ir_path,
        **stats,
    }
    if quiet:
        out["quiet"] = True
    return out


# --------------------------------------------------------------------------
# Hybrid path: prepare / finalize
# --------------------------------------------------------------------------
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_A_RE = re.compile(r'<a href="[^"]*">(.*?)</a>')


def _digest_text(text: str, limit: int) -> str:
    """Squeeze a briefing into a continuity digest: drop the archive section,
    strip link URLs (Google News links run to hundreds of chars each) and
    collapse blank lines. Pure so it is testable."""
    text = text.split("## 아카이브", 1)[0]
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _HTML_A_RE.sub(r"\1", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text[:limit]


def _yesterday_digest() -> str:
    """Yesterday's committed briefing as a link-free digest (for day-over-day
    continuity). Empty string if none exists or the budget is 0 (the default
    since whats_changed became optional)."""
    if config.YESTERDAY_DIGEST_CHARS <= 0:
        return ""
    y = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
    p = config.BRIEFS_DIR / f"{y}.md"
    if not p.exists():
        return ""
    try:
        return _digest_text(p.read_text(encoding="utf-8"), config.YESTERDAY_DIGEST_CHARS)
    except OSError:
        return ""


def _dump_agent_view(payload: dict) -> str:
    """Compact JSON with one candidate per line — no indentation overhead, but
    still diffable/readable line by line for the agent."""
    cands = payload.pop("candidates")
    head = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    rows = ",\n  ".join(json.dumps(c, ensure_ascii=False, separators=(",", ":"))
                        for c in cands)
    return head[:-1] + ',"candidates":[\n  ' + rows + "\n]}"


def run_prepare() -> dict:
    """Build the candidate pool. Writes two files:
    - state/candidates.json — slim agent view (no URLs, 120-char snippets,
      category + indicators), the only file the lead agent should read;
    - state/pool.json — full Articles for --finalize to round-trip.

    조용한 날(관련 기사 0건)에도 실패가 아니다: 두 파일 모두 ``quiet: true`` +
    빈 후보로 쓰고, 에이전트는 선별 없이 바로 --finalize를 돌린다(0건 브리핑)."""
    fresh, failed, irrelevant = _build_candidates()
    quiet = not fresh
    agent_view = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "failed_sources": failed,
        "diversity_caps": {"per_source": config.MAX_PER_SOURCE},
        "max_items": MAX_ITEMS,
        "indicator_keys": list(relevance.INDICATOR_KEYS),
    }
    if quiet:
        agent_view["quiet"] = True
    digest = _yesterday_digest()
    if digest:
        agent_view["yesterday_digest"] = digest
    agent_view["candidates"] = [a.to_llm_dict(i, snippet_chars=config.SNIPPET_CHARS)
                                for i, a in enumerate(fresh)]
    machine = {
        "failed_sources": failed,
        "candidates": [{"id": i, **a.to_dict()} for i, a in enumerate(fresh)],
    }
    if quiet:
        machine["quiet"] = True
    config.ensure_dirs()
    CANDIDATES_PATH.write_text(_dump_agent_view(agent_view), encoding="utf-8")
    POOL_PATH.write_text(json.dumps(machine, ensure_ascii=False,
                                    separators=(",", ":")), encoding="utf-8")
    out = {"mode": "prepare", "candidates": len(fresh), "failed_sources": failed,
           "irrelevant_dropped": irrelevant, "path": str(CANDIDATES_PATH)}
    if quiet:
        out["quiet"] = True
    return out


def _load_candidates() -> Tuple[List[Article], List[str], bool]:
    data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    arts = [Article.from_dict(c) for c in data.get("candidates", [])]
    return arts, data.get("failed_sources", []), bool(data.get("quiet"))


def _apply_agent_selection(candidates: List[Article]) -> Optional[List[Article]]:
    """Apply the lead agent's state/selection.json. Returns selected+summarised
    articles, or None if the file is missing/invalid (caller falls back to the
    autonomous select+summarize).

    새 계약(2026-11-04)의 행은 id·title·one_liner·indicators·confidence·flags뿐이다.
    구형 행(why_it_matters·implications·tags·evidence 포함)도 그대로 받는다 —
    해석은 summarize.apply_item 한 곳."""
    if not SELECTION_PATH.exists():
        return None
    try:
        data = json.loads(SELECTION_PATH.read_text(encoding="utf-8"))
        items = data.get("selected") or []
    except (json.JSONDecodeError, OSError):
        return None
    if not items:
        return None

    by_id = {i: a for i, a in enumerate(candidates)}
    ordered_ids: List[int] = []
    for item in items:
        try:
            cid = int(item["id"])
        except (KeyError, ValueError, TypeError):
            continue
        a = by_id.get(cid)
        if a is None or not isinstance(item, dict):
            continue
        summarize.apply_item(a, item)
        ordered_ids.append(cid)

    if not ordered_ids:
        return None
    # Enforce diversity caps as a safety net even on the agent's choices.
    return select._apply_caps(ordered_ids, candidates, MAX_ITEMS)


def _str_list(v) -> List[str]:
    if isinstance(v, str):
        v = [v]
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def _load_agent_meta() -> dict:
    """Read the briefing-level fields the lead agent may add to selection.json:
    events (새 계약의 유일한 브리핑 레벨 필드), and — optional, old format —
    top_insight (관전 포인트), whats_changed (연속성), themes (핵심 테마).
    빠진 키는 빈 목록이 되고 렌더가 해당 섹션을 생략한다."""
    empty = {"events": [], "top_insight": [], "whats_changed": [], "themes": []}
    if not SELECTION_PATH.exists():
        return empty
    try:
        data = json.loads(SELECTION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty
    return {
        "events": _str_list(data.get("events")),
        "top_insight": _str_list(data.get("top_insight")),
        "whats_changed": _str_list(data.get("whats_changed")),
        "themes": _str_list(data.get("themes")),
    }


def run_finalize(persist: bool = True) -> dict:
    """Render + archive using candidates.json and (if present) the agent's
    selection.json. Falls back to autonomous select+summarize when the agent
    did not provide a selection."""
    if not POOL_PATH.exists():
        raise RuntimeError("state/pool.json missing; run --prepare first")
    candidates, failed, quiet = _load_candidates()
    if quiet and not candidates:
        # 조용한 날: 0건이지만 정상 브리핑(degraded 아님). selection.json은 볼 것이 없다.
        _tg, markdown, stats = render.render_briefing([], failed_sources=failed, quiet=True)
        ir = render.render_ir([], failed_sources=failed, quiet=True)
        brief_path, ir_path = _deliver(ir, markdown, [], persist=persist)
        return {"mode": "finalize", "selection": "quiet", "quiet": True,
                "failed_sources": failed, "brief_path": brief_path,
                "ir_path": ir_path, **stats}
    if not candidates:
        raise RuntimeError("no candidates to finalize")

    selected = _apply_agent_selection(candidates)
    method = "agent"
    meta = _load_agent_meta()
    if selected is None:
        selected, sel_method = select.select(candidates, max_items=MAX_ITEMS)
        summarize.summarize(selected)
        method = f"autonomous:{sel_method}"

    _tg, markdown, stats = render.render_briefing(
        selected, failed_sources=failed, events=meta["events"],
        top_insight=meta["top_insight"], whats_changed=meta["whats_changed"],
        themes=meta["themes"])
    ir = render.render_ir(
        selected, failed_sources=failed, events=meta["events"],
        top_insight=meta["top_insight"], whats_changed=meta["whats_changed"],
        themes=meta["themes"])
    brief_path, ir_path = _deliver(ir, markdown, selected, persist=persist)
    return {"mode": "finalize", "selection": method, "failed_sources": failed,
            "brief_path": brief_path, "ir_path": ir_path, **stats}


# --------------------------------------------------------------------------
# Top-level with fallback
# --------------------------------------------------------------------------
def run_fallback_path(reason: str, persist: bool = True) -> dict:
    """Deterministic fallback: gather headlines, archive them as markdown and
    page IR. ``persist=False`` (--dry-run) builds everything but writes nothing."""
    print(f"[pipeline] main path failed: {reason}")
    from . import fallback
    # Skip anything selected previously, so a fallback after a good run does not
    # resurface stale headlines.
    sent = state.load_sent_log()
    msg, items = fallback.run_fallback(sent_log=sent)
    now = datetime.now(KST)
    date_str = now.strftime("%Y-%m-%d")
    if not persist:
        return {"mode": "fallback", "reason": reason, "message_len": len(msg),
                "persisted": False, "items": len(items)}
    try:
        config.ensure_dirs()
        (config.BRIEFS_DIR / f"{date_str}.md").write_text(
            f"# 뉴스 브리핑 (폴백) — {date_str}\n\n{msg}\n", encoding="utf-8")
        # The page must say "this is a fallback day" rather than look like a
        # thin normal day, so the IR carries the degraded flag explicitly.
        ir = render.fallback_ir(items, now=now, reason=reason)
        (config.BRIEFS_DIR / f"{date_str}.json").write_text(
            json.dumps(ir, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return {"mode": "fallback", "reason": reason, "message_len": len(msg),
            "persisted": True, "items": len(items)}


def run(persist: bool = True) -> dict:
    """Autonomous run with automatic deterministic fallback."""
    try:
        return run_pipeline(persist=persist)
    except Exception as exc:  # noqa: BLE001 - fallback must catch everything
        traceback.print_exc()
        return run_fallback_path(str(exc), persist=persist)


def run_finalize_safe(persist: bool = True) -> dict:
    """Hybrid finalize with automatic deterministic fallback."""
    try:
        return run_finalize(persist=persist)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return run_fallback_path(str(exc), persist=persist)
