"""Telegram delivery with 4096-char splitting.

Sends to @realneeewsbot using TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from env.
Long briefings are split on section/line boundaries so each chunk stays under
the 4096-char limit, then sent sequentially.
"""

from __future__ import annotations

import time
from typing import List

from . import config, http


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


def send_message(text: str, *, disable_preview: bool = True) -> bool:
    """Send a (possibly long) message. Returns True if all chunks delivered."""
    missing = config.missing_required_secrets()
    if missing:
        # Cannot deliver — surface clearly to the caller/logs.
        print(f"[telegram] missing secrets: {', '.join(missing)}; message not sent")
        return False

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = split_message(text)
    ok = True
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": disable_preview,
        }
        resp = http.session().post(url, data=payload, timeout=config.HTTP_TIMEOUT)
        if resp is None or resp.status_code != 200:
            ok = False
            print(f"[telegram] chunk {i+1}/{len(chunks)} failed: "
                  f"{getattr(resp, 'status_code', 'no-response')}")
        if i < len(chunks) - 1:
            time.sleep(0.5)  # be gentle with rate limits
    return ok
