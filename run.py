#!/usr/bin/env python3
"""Entry point for the daily news briefing.

Autonomous (GitHub Actions path):
    python run.py                 # full pipeline (fallback on failure)
    python run.py --dry-run       # build briefing but write nothing
    python run.py --fallback      # force the deterministic fallback path
    python run.py --check         # validate config/feeds

Hybrid (Claude scheduled session / subscription model — see ORCHESTRATION.md):
    python run.py --prepare       # collect+dedup -> state/candidates.json
    python run.py --finalize      # render+archive using agent's selection.json

Each run writes two files to briefs/: <date>.md (invest-wiki ingest source) and
<date>.json (report-page IR). Nothing is sent anywhere — the report page is the
delivery channel as of 2026-08-15.

Designed to ALWAYS exit 0 after attempting the archive (fallback included) so
the scheduler never reports a hard failure for a soft data problem.
"""

from __future__ import annotations

import argparse
import json
import sys

from news import config, pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily morning news briefing")
    parser.add_argument("--dry-run", action="store_true", help="build but write nothing")
    parser.add_argument("--fallback", action="store_true", help="force fallback path")
    parser.add_argument("--check", action="store_true", help="validate setup only")
    parser.add_argument("--prepare", action="store_true", help="hybrid: build candidates.json")
    parser.add_argument("--finalize", action="store_true", help="hybrid: render+archive from selection.json")
    args = parser.parse_args()

    config.ensure_dirs()

    if args.check:
        print(json.dumps({
            "newsdata_key": bool(config.NEWSDATA_API_KEY),
            "guardian_key": bool(config.GUARDIAN_API_KEY),
            "anthropic_key": bool(config.ANTHROPIC_API_KEY),
            "model": config.ANTHROPIC_MODEL,
        }, ensure_ascii=False, indent=2))
        return 0

    persist = not args.dry_run

    if args.fallback:
        result = pipeline.run_fallback_path("forced by --fallback", persist=persist)
    elif args.prepare:
        try:
            result = pipeline.run_prepare()
        except Exception as exc:  # noqa: BLE001 - report, let agent run --fallback
            result = {"mode": "prepare-failed", "reason": str(exc)}
    elif args.finalize:
        result = pipeline.run_finalize_safe(persist=persist)
    else:
        result = pipeline.run(persist=persist)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
