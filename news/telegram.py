"""Telegram delivery with 4096-char splitting.

Sends to @realneeewsbot using TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from env.
Long briefings are split on section/line boundaries so each chunk stays under
the 4096-char limit, then sent sequentially.
"""

from __future__ import annotations

import time
from typing import List, NamedTuple

from . import config, http


class SendResult(NamedTuple):
    ok: bool        # every chunk delivered
    sent: int       # chunks actually delivered
    total: int      # chunks attempted

    @property
    def any_sent(self) -> bool:
        return self.sent > 0


def split_message(text: str, limit: int = config.TELEGRAM_MAX_CHARS) -> List[str]:
    """Split on blank-line (section) boundaries first, then lines, then hard."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.rstrip())
        current = ""

    for block in text.split("\n\n"):
        candidate = (current + "\n\n" + block) if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        flush()
        if len(block) <= limit:
            current = block
            continue
        # Block itself too big: split by lines.
        for line in block.split("\n"):
            cand2 = (current + "\n" + line) if current else line
            if len(cand2) <= limit:
                current = cand2
            else:
                flush()
                # Single line longer than limit: hard-chunk it.
                while len(line) > limit:
                    chunks.append(line[:limit])
                    line = line[limit:]
                current = line
    flush()
    return chunks or [text[:limit]]


def _post_chunk(url: str, payload: dict, retries: int = 3) -> bool:
    """POST one chunk with exponential-backoff retries (GET has retries; the
    final delivery step must too, so a transient blip doesn't cascade to the
    fallback)."""
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            resp = http.session().post(url, data=payload, timeout=config.HTTP_TIMEOUT)
            if resp is not None and resp.status_code == 200:
                return True
            # 4xx (bad token/chat) won't fix on retry — stop early.
            if resp is not None and 400 <= resp.status_code < 500 and resp.status_code != 429:
                print(f"[telegram] non-retryable status {resp.status_code}")
                return False
        except Exception:  # noqa: BLE001 - treat transport errors as retryable
            pass
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    return False


def send_message(text: str, *, disable_preview: bool = True) -> SendResult:
    """Send a (possibly long) message, splitting into chunks. Each chunk is
    retried independently. Returns a SendResult so callers can distinguish
    'nothing sent' from 'partially sent' (which must NOT trigger a re-send)."""
    missing = config.missing_required_secrets()
    if missing:
        print(f"[telegram] missing secrets: {', '.join(missing)}; message not sent")
        return SendResult(ok=False, sent=0, total=0)

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = split_message(text)
    sent = 0
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": disable_preview,
        }
        if _post_chunk(url, payload):
            sent += 1
        else:
            print(f"[telegram] chunk {i+1}/{len(chunks)} failed after retries")
        if i < len(chunks) - 1:
            time.sleep(0.5)  # be gentle with rate limits
    return SendResult(ok=(sent == len(chunks)), sent=sent, total=len(chunks))
