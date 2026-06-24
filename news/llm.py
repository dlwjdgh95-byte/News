"""Thin Anthropic Messages API client (HTTP, no SDK dependency).

Uses a stable, subscribed model (config.ANTHROPIC_MODEL) to avoid free-tier 429s.
If ANTHROPIC_API_KEY is absent or a call fails, callers fall back to deterministic
heuristics — the briefing still goes out, just less polished.

We make at most two LLM calls per run: one batched SELECTION call and one batched
SUMMARY call, to minimise cost and failure surface.
"""

from __future__ import annotations

import json
from typing import Optional

from . import config, http


class LLMUnavailable(Exception):
    pass


def available() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def complete_json(system: str, user: str, *, max_tokens: int = 4000) -> Optional[dict]:
    """Send one prompt, expect a JSON object back. Returns parsed dict or None."""
    if not available():
        return None
    url = f"{config.ANTHROPIC_BASE_URL}/v1/messages"
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    try:
        resp = http.session().post(url, headers=headers, data=json.dumps(body),
                                   timeout=max(config.HTTP_TIMEOUT, 60))
    except Exception:  # noqa: BLE001 - never let LLM transport break the run
        return None
    if resp is None or resp.status_code != 200:
        print(f"[llm] request failed: {getattr(resp, 'status_code', 'no-response')}")
        return None
    try:
        data = resp.json()
        parts = data.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    except (ValueError, AttributeError):
        return None
    return _extract_json(text)


def _extract_json(text: str) -> Optional[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
