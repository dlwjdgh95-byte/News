"""Orchestration: collect (A·B·C in parallel) -> normalize -> prefilter ->
dedup -> drop already-sent -> select (1 LLM call + caps) -> summarize (1 LLM
call + evidence) -> render -> Telegram -> archive commit.

Any failure / timeout / empty result switches to the deterministic fallback so a
minimal briefing always arrives at 07:30 KST.
"""

from __future__ import annotations

import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

from . import config, state, normalize, prefilter, dedup, select, summarize, render
from .model import Article, SOURCE_A, SOURCE_B, SOURCE_C

KST = timezone(timedelta(hours=9))
COLLECT_TIMEOUT = 90  # seconds per source


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


def run_pipeline(send: bool = True) -> dict:
    """Execute the main pipeline. Raises on unrecoverable failure so the caller
    (run.py) can switch to the fallback path."""
    config.ensure_dirs()

    raw, failed = _collect_all()
    if not raw:
        raise RuntimeError(f"collection produced no articles (failed={failed})")

    arts = normalize.normalize(raw)
    arts, dropped = prefilter.prefilter(arts)
    reps = dedup.deduplicate(arts)

    # Drop anything already sent on a previous day.
    log = state.load_sent_log()
    fresh = [a for a in reps if not state.already_sent(log, a.canonical_url)]
    if not fresh:
        raise RuntimeError("all candidates already sent previously")

    selected, sel_method = select.select(fresh, max_items=14)
    sum_method = summarize.summarize(selected)

    telegram_text, markdown, stats = render.render_briefing(
        selected, failed_sources=failed)

    sent_ok = True
    if send:
        from . import telegram
        sent_ok = telegram.send_message(telegram_text)
        if not sent_ok:
            raise RuntimeError("telegram delivery failed")

    # Archive commit artefacts.
    now = datetime.now(KST)
    brief_path = config.BRIEFS_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    brief_path.write_text(markdown, encoding="utf-8")

    # Record only what we actually pushed.
    pushed_urls = [a.canonical_url for a in selected
                   if a.confidence >= render.PUSH_CONFIDENCE_MIN]
    state.record_sent(log, pushed_urls, now)
    state.prune_sent_log(log)
    state.save_sent_log(log)

    return {
        "mode": "main",
        "collected": len(raw),
        "after_filter": len(arts),
        "prefiltered_out": dropped,
        "clusters": len(reps),
        "selection": sel_method,
        "summary": sum_method,
        "failed_sources": failed,
        "brief_path": str(brief_path),
        "sent_ok": sent_ok,
        **stats,
    }


def run(send: bool = True) -> dict:
    """Top-level: try the main pipeline, fall back deterministically on any error."""
    try:
        return run_pipeline(send=send)
    except Exception as exc:  # noqa: BLE001 - fallback must catch everything
        print(f"[pipeline] main path failed: {exc}")
        traceback.print_exc()
        from . import fallback
        msg = fallback.run_fallback(send=send)
        # Archive the fallback too.
        try:
            config.ensure_dirs()
            now = datetime.now(KST)
            (config.BRIEFS_DIR / f"{now.strftime('%Y-%m-%d')}.md").write_text(
                f"# 뉴스 브리핑 (폴백) — {now.strftime('%Y-%m-%d')}\n\n{msg}\n",
                encoding="utf-8")
        except OSError:
            pass
        return {"mode": "fallback", "reason": str(exc), "message_len": len(msg)}
