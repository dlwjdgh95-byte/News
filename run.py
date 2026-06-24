#!/usr/bin/env python3
"""Entry point for the daily news briefing.

Autonomous (GitHub Actions / API-key path):
    python run.py                 # full pipeline + send (fallback on failure)
    python run.py --dry-run       # build briefing but do NOT send / persist
    python run.py --fallback      # force the deterministic fallback path
    python run.py --check         # validate config/feeds, no send

Hybrid (Claude scheduled session / subscription model — see ORCHESTRATION.md):
    python run.py --prepare       # collect+dedup -> state/candidates.json
    python run.py --finalize      # render+send using agent's state/selection.json

Designed to ALWAYS exit 0 after attempting delivery (fallback included) so the
scheduler never reports a hard failure for a soft data problem.
"""

from __future__ import annotations

import argparse
import json
import sys

from news import config, pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily morning news briefing")
    parser.add_argument("--dry-run", action="store_true", help="do not send or persist state")
    parser.add_argument("--fallback", action="store_true", help="force fallback path")
    parser.add_argument("--check", action="store_true", help="validate setup only")
    parser.add_argument("--prepare", action="store_true", help="hybrid: build candidates.json")
    parser.add_argument("--finalize", action="store_true", help="hybrid: render+send from selection.json")
    args = parser.parse_args()

    config.ensure_dirs()

    if args.check:
        missing = config.missing_required_secrets()
        print(json.dumps({
            "telegram_ready": not missing,
            "missing_required": missing,
            "newsdata_key": bool(config.NEWSDATA_API_KEY),
            "guardian_key": bool(config.GUARDIAN_API_KEY),
            "anthropic_key": bool(config.ANTHROPIC_API_KEY),
            "model": config.ANTHROPIC_MODEL,
        }, ensure_ascii=False, indent=2))
        return 0

    send = not args.dry_run
    persist = not args.dry_run

    if args.fallback:
        from news import fallback, state
        msg = fallback.run_fallback(send=send, sent_log=state.load_sent_log())
        print(msg if args.dry_run else "[fallback sent]")
        return 0

    if args.prepare:
        try:
            result = pipeline.run_prepare()
        except Exception as exc:  # noqa: BLE001 - report, let agent run --fallback
            result = {"mode": "prepare-failed", "reason": str(exc)}
    elif args.finalize:
        result = pipeline.run_finalize_safe(send=send, persist=persist)
    else:
        result = pipeline.run(send=send, persist=persist)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
