"""Orchestration.

Two ways to run the intelligence (selection + summary) step:

  * Autonomous (run_pipeline): collect -> normalize -> prefilter -> dedup ->
    drop-sent -> select (1 LLM call + caps) -> summarize (1 LLM call) -> render
    -> Telegram -> archive. Used by the GitHub Actions / API-key path.

  * Hybrid (run_prepare + run_finalize): `run_prepare` does everything up to the
    candidate pool and writes state/candidates.json; a lead agent (subscription
    model) writes state/selection.json with the single-pass selection+summary;
    `run_finalize` renders, delivers and archives. See ORCHESTRATION.md.

Any failure / timeout / empty result switches to the deterministic fallback so a
minimal briefing always arrives at 07:30 KST.

Double-send safety (B1): if Telegram delivers ANY chunk of the main briefing we
do NOT trigger the fallback (which would send a second message). The fallback
runs only when nothing was delivered.
"""

from __future__ import annotations

import json
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from . import config, state, normalize, prefilter, dedup, select, summarize, render
from .model import Article, SOURCE_A, SOURCE_B, SOURCE_C

KST = timezone(timedelta(hours=9))
COLLECT_TIMEOUT = 90  # seconds per source
# Agent-facing candidate view (slim, no URLs) vs full machine state. The lead
# agent reads candidates.json into its context, so that file is kept lean;
# pool.json round-trips the complete Articles for --finalize.
CANDIDATES_PATH = config.STATE_DIR / "candidates.json"
POOL_PATH = config.STATE_DIR / "pool.json"
SELECTION_PATH = config.STATE_DIR / "selection.json"
MAX_ITEMS = 14


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


def _build_candidates() -> Tuple[List[Article], List[str]]:
    """Collect -> normalize -> prefilter -> dedup -> drop already-sent.

    Only a genuinely empty collection raises (that is what the fallback is for).
    Each transform is guarded so one bad article can't crash the run, and if the
    sent-log filter removes everything we keep the freshest deduped items rather
    than forcing a fallback — a slightly-repeated item beats an ugly fallback."""
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
    log = state.load_sent_log()
    fresh = [a for a in reps if not state.already_sent(log, a.canonical_url)]
    if not fresh:
        # Everything was sent before — don't fall back; keep the freshest few.
        fresh = reps[:MAX_ITEMS]
    return _cap_pool(fresh, config.MAX_CANDIDATES), failed


def _cap_pool(arts: List[Article], cap: int) -> List[Article]:
    """Bound the candidate pool that reaches the LLM/agent. Within each source
    tag articles are kept in heuristic (confidence+recency) order, then tags are
    drained round-robin so no tag can crowd out the others."""
    if len(arts) <= cap:
        return arts
    order = select._heuristic_order(arts)
    by_tag: dict[str, List[Article]] = {SOURCE_A: [], SOURCE_B: [], SOURCE_C: []}
    for i in order:
        by_tag[arts[i].source_tag].append(arts[i])
    queues = [q for q in (by_tag[SOURCE_A], by_tag[SOURCE_B], by_tag[SOURCE_C]) if q]
    out: List[Article] = []
    while len(out) < cap and queues:
        for q in queues:
            if q and len(out) < cap:
                out.append(q.pop(0))
        queues = [q for q in queues if q]
    return out


def _deliver(telegram_text: str, markdown: str, selected: List[Article],
             *, send: bool, persist: bool):
    """Send the briefing and archive. Raises only when NOTHING was delivered
    (so the caller can fall back). Partial/full delivery never falls back."""
    if send:
        from . import telegram
        result = telegram.send_message(telegram_text)
        if not result.any_sent:
            raise RuntimeError("telegram delivery failed entirely")
    else:
        from .telegram import SendResult
        result = SendResult(ok=True, sent=0, total=0)

    now = datetime.now(KST)
    brief_path = config.BRIEFS_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    brief_path.write_text(markdown, encoding="utf-8")

    # Record only what we actually pushed, and only when we really delivered.
    if send and persist:
        log = state.load_sent_log()
        pushed_urls = [a.canonical_url for a in selected
                       if a.confidence >= render.PUSH_CONFIDENCE_MIN]
        state.record_sent(log, pushed_urls, now)
        state.prune_sent_log(log)
        state.save_sent_log(log)
    return result, str(brief_path)


# --------------------------------------------------------------------------
# Autonomous path
# --------------------------------------------------------------------------
def run_pipeline(send: bool = True, persist: bool = True) -> dict:
    """Execute the full autonomous pipeline. Raises on unrecoverable failure so
    run() can switch to the fallback path."""
    fresh, failed = _build_candidates()
    selected, sel_method = select.select(fresh, max_items=MAX_ITEMS)
    sum_method = summarize.summarize(selected)
    telegram_text, markdown, stats = render.render_briefing(selected, failed_sources=failed)
    result, brief_path = _deliver(telegram_text, markdown, selected, send=send, persist=persist)
    return {
        "mode": "main",
        "selection": sel_method,
        "summary": sum_method,
        "failed_sources": failed,
        "brief_path": brief_path,
        "delivered": f"{result.sent}/{result.total}",
        **stats,
    }


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
    continuity). Empty string if none exists."""
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
    - state/candidates.json — slim agent view (no URLs, truncated snippets),
      the only file the lead agent should read;
    - state/pool.json — full Articles for --finalize to round-trip."""
    fresh, failed = _build_candidates()
    agent_view = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "failed_sources": failed,
        "diversity_caps": {"per_source": config.MAX_PER_SOURCE,
                           "per_cluster": config.MAX_PER_CLUSTER},
        "max_items": MAX_ITEMS,
        "yesterday_digest": _yesterday_digest(),
        "candidates": [a.to_llm_dict(i, snippet_chars=config.SNIPPET_CHARS)
                       for i, a in enumerate(fresh)],
    }
    machine = {
        "failed_sources": failed,
        "candidates": [{"id": i, **a.to_dict()} for i, a in enumerate(fresh)],
    }
    config.ensure_dirs()
    CANDIDATES_PATH.write_text(_dump_agent_view(agent_view), encoding="utf-8")
    POOL_PATH.write_text(json.dumps(machine, ensure_ascii=False,
                                    separators=(",", ":")), encoding="utf-8")
    return {"mode": "prepare", "candidates": len(fresh), "failed_sources": failed,
            "path": str(CANDIDATES_PATH)}


def _load_candidates() -> Tuple[List[Article], List[str]]:
    data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    arts = [Article.from_dict(c) for c in data.get("candidates", [])]
    return arts, data.get("failed_sources", [])


def _apply_agent_selection(candidates: List[Article]) -> Optional[List[Article]]:
    """Apply the lead agent's state/selection.json. Returns selected+summarised
    articles, or None if the file is missing/invalid (caller falls back to the
    autonomous select+summarize)."""
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
        if a is None:
            continue
        a.one_liner = (item.get("one_liner") or "").strip()
        a.why_it_matters = (item.get("why_it_matters") or "").strip()
        a.implications = (item.get("implications") or "").strip()
        a.tags = [str(t) for t in (item.get("tags") or [])][:5]
        try:
            a.confidence = max(0.0, min(1.0, float(item.get("confidence", a.confidence))))
        except (ValueError, TypeError):
            pass
        a.evidence = (item.get("evidence") or "").strip()
        for f in (item.get("flags") or []):
            if f and f not in a.flags:
                a.flags.append(str(f))
        summarize._apply_translation(a, (item.get("title") or "").strip())
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
    """Read the optional briefing-level fields the lead agent may add to
    selection.json: events, top_insight (관전 포인트), whats_changed (연속성),
    themes (핵심 테마)."""
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


def run_finalize(send: bool = True, persist: bool = True) -> dict:
    """Render + deliver + archive using candidates.json and (if present) the
    agent's selection.json. Falls back to autonomous select+summarize when the
    agent did not provide a selection."""
    if not POOL_PATH.exists():
        raise RuntimeError("state/pool.json missing; run --prepare first")
    candidates, failed = _load_candidates()
    if not candidates:
        raise RuntimeError("no candidates to finalize")

    selected = _apply_agent_selection(candidates)
    method = "agent"
    meta = _load_agent_meta()
    if selected is None:
        selected, sel_method = select.select(candidates, max_items=MAX_ITEMS)
        summarize.summarize(selected)
        method = f"autonomous:{sel_method}"

    telegram_text, markdown, stats = render.render_briefing(
        selected, failed_sources=failed, events=meta["events"],
        top_insight=meta["top_insight"], whats_changed=meta["whats_changed"],
        themes=meta["themes"])
    result, brief_path = _deliver(telegram_text, markdown, selected, send=send, persist=persist)
    return {"mode": "finalize", "selection": method, "failed_sources": failed,
            "brief_path": brief_path, "delivered": f"{result.sent}/{result.total}", **stats}


# --------------------------------------------------------------------------
# Top-level with fallback
# --------------------------------------------------------------------------
def _run_fallback(reason: str, send: bool) -> dict:
    print(f"[pipeline] main path failed: {reason}")
    from . import fallback
    # Skip anything already pushed previously, to avoid re-sending stale items.
    sent = state.load_sent_log()
    msg = fallback.run_fallback(send=send, sent_log=sent)
    try:
        config.ensure_dirs()
        now = datetime.now(KST)
        (config.BRIEFS_DIR / f"{now.strftime('%Y-%m-%d')}.md").write_text(
            f"# 뉴스 브리핑 (폴백) — {now.strftime('%Y-%m-%d')}\n\n{msg}\n", encoding="utf-8")
    except OSError:
        pass
    return {"mode": "fallback", "reason": reason, "message_len": len(msg)}


def run(send: bool = True, persist: bool = True) -> dict:
    """Autonomous run with automatic deterministic fallback."""
    try:
        return run_pipeline(send=send, persist=persist)
    except Exception as exc:  # noqa: BLE001 - fallback must catch everything
        traceback.print_exc()
        return _run_fallback(str(exc), send)


def run_finalize_safe(send: bool = True, persist: bool = True) -> dict:
    """Hybrid finalize with automatic deterministic fallback."""
    try:
        return run_finalize(send=send, persist=persist)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return _run_fallback(str(exc), send)
