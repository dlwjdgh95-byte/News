"""Repo-backed state: sent-log de-duplication across days + query cache.

The cloud routine re-clones the repo every run, so all cross-run memory must be
committed files under ``state/``. We record yesterday's sent articles by
canonical URL and skip anything already sent.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import config


def load_sent_log() -> dict:
    p = config.SENT_LOG_PATH
    if not p.exists():
        return {"sent": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if "sent" not in data:
            data["sent"] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return {"sent": {}}


def already_sent(log: dict, canonical_url: str) -> bool:
    return bool(canonical_url) and canonical_url in log.get("sent", {})


def record_sent(log: dict, canonical_urls: Iterable[str], when: datetime | None = None) -> None:
    when = when or datetime.now(timezone.utc)
    stamp = when.isoformat()
    for u in canonical_urls:
        if u:
            log["sent"][u] = stamp


def prune_sent_log(log: dict, keep_days: int = 14) -> None:
    """Keep the log small — only the last ``keep_days`` of sent URLs."""
    cutoff = time.time() - keep_days * 86400
    kept = {}
    for url, stamp in log.get("sent", {}).items():
        try:
            ts = datetime.fromisoformat(stamp).timestamp()
        except (ValueError, TypeError):
            ts = time.time()
        if ts >= cutoff:
            kept[url] = stamp
    log["sent"] = kept


def save_sent_log(log: dict) -> None:
    config.ensure_dirs()
    config.SENT_LOG_PATH.write_text(
        json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


# --- Query cache (saves NewsData.io / HTTP calls within a run/day) ---------
def _cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return config.CACHE_DIR / f"{h}.json"


def cache_get(key: str, ttl_seconds: int = 3600) -> object | None:
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - blob.get("_ts", 0) > ttl_seconds:
        return None
    return blob.get("value")


def cache_set(key: str, value: object) -> None:
    config.ensure_dirs()
    p = _cache_path(key)
    try:
        p.write_text(json.dumps({"_ts": time.time(), "value": value},
                                ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
