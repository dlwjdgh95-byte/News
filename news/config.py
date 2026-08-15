"""Configuration and secrets.

All secrets are read from environment variables — never hard-coded. The code is
written so that a missing optional secret degrades gracefully rather than
crashing the whole run (the deterministic fallback must always be able to send).
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Paths (all state lives in the repo, backend is stateless) -------------
REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
BRIEFS_DIR = REPO_ROOT / "briefs"
CACHE_DIR = REPO_ROOT / "cache"
SENT_LOG_PATH = STATE_DIR / "sent_log.json"

# --- Secrets (env only) ----------------------------------------------------
# No Telegram credentials any more: the briefing is delivered as a report page,
# and the surviving failure alerts live in Miner's invest-wiki/scripts/notify.py.
NEWSDATA_API_KEY = os.environ.get("NEWSDATA_API_KEY", "")
GUARDIAN_API_KEY = os.environ.get("GUARDIAN_API_KEY", "")  # optional quality boost
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- LLM settings ----------------------------------------------------------
# Use a stable, subscribed model to avoid free-tier 429 rate limits.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_API_BASE", "https://api.anthropic.com")

# --- Behaviour knobs -------------------------------------------------------
# NewsData.io credit discipline: ~3 requests/day, ~30 items total.
NEWSDATA_MAX_REQUESTS = int(os.environ.get("NEWSDATA_MAX_REQUESTS", "3"))
NEWSDATA_PAGE_SIZE = int(os.environ.get("NEWSDATA_PAGE_SIZE", "10"))

# Dedup thresholds.
JACCARD_TITLE_THRESHOLD = 0.6
TIME_SPLIT_HOURS = 12  # >= this gap + a status-change cue => keep as separate follow-up

# Selection diversity caps.
MAX_PER_SOURCE = 2
MAX_PER_CLUSTER = 2

# Token discipline: cap the candidate pool that reaches the LLM/agent (balanced
# across source tags) and the per-article snippet length in LLM payloads.
MAX_CANDIDATES = int(os.environ.get("NEWS_MAX_CANDIDATES", "40"))
SNIPPET_CHARS = int(os.environ.get("NEWS_SNIPPET_CHARS", "400"))
# Yesterday-digest budget for day-over-day continuity (chars, links stripped).
YESTERDAY_DIGEST_CHARS = int(os.environ.get("NEWS_YESTERDAY_DIGEST_CHARS", "1500"))

# Fallback: ~10 freshest headlines per feed group.
FALLBACK_ITEMS = 10

# Network.
HTTP_TIMEOUT = float(os.environ.get("NEWS_HTTP_TIMEOUT", "15"))
HTTP_RETRIES = 3
USER_AGENT = "DailyNewsBriefing/1.0 (+https://github.com/dlwjdgh95-byte/news)"


def ensure_dirs() -> None:
    for d in (STATE_DIR, BRIEFS_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
