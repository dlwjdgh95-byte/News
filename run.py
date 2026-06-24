#!/usr/bin/env python3
"""Entry point for the daily news briefing.

Usage:
    python run.py                 # full pipeline, send to Telegram (fallback on failure)
    python run.py --dry-run       # build briefing but do NOT send to Telegram
    python run.py --fallback      # force the deterministic fallback path
    python run.py --check         # validate config/feeds, no send

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
    parser.add_argument("--dry-run", action="store_true", help="do not send to Telegram")
    parser.add_argument("--fallback", action="store_true", help="force fallback path")
    parser.add_argument("--check", action="store_true", help="validate setup only")
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

    if args.fallback:
        from news import fallback
        msg = fallback.run_fallback(send=send)
        print(msg if args.dry_run else "[fallback sent]")
        return 0

    result = pipeline.run(send=send)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
