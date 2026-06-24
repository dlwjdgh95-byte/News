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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NEWSDATA_API_KEY = os.environ.get("NEWSDATA_API_KEY", "")
GUARDIAN_API_KEY = os.environ.get("GUARDIAN_API_KEY", "")  # optional quality boost
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Telegram bot identity (for documentation/sanity only): @realneeewsbot

# --- LLM settings ----------------------------------------------------------
# Use a stable, subscribed model to avoid free-tier 429 rate limits.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_API_BASE", "https://api.anthropic.com")

# --- Behaviour knobs -------------------------------------------------------
TELEGRAM_MAX_CHARS = 4096

# NewsData.io credit discipline: ~3 requests/day, ~30 items total.
NEWSDATA_MAX_REQUESTS = int(os.environ.get("NEWSDATA_MAX_REQUESTS", "3"))
NEWSDATA_PAGE_SIZE = int(os.environ.get("NEWSDATA_PAGE_SIZE", "10"))

# Dedup thresholds.
JACCARD_TITLE_THRESHOLD = 0.6
TIME_SPLIT_HOURS = 12  # >= this gap + a status-change cue => keep as separate follow-up

# Selection diversity caps.
MAX_PER_SOURCE = 2
MAX_PER_CLUSTER = 2

# Fallback: ~10 freshest headlines per feed group.
FALLBACK_ITEMS = 10

# Network.
HTTP_TIMEOUT = float(os.environ.get("NEWS_HTTP_TIMEOUT", "15"))
HTTP_RETRIES = 3
USER_AGENT = "DailyNewsBriefing/1.0 (+https://github.com/dlwjdgh95-byte/news)"


def missing_required_secrets() -> list[str]:
    """Secrets required even for the deterministic fallback to deliver."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    return missing


def ensure_dirs() -> None:
    for d in (STATE_DIR, BRIEFS_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
