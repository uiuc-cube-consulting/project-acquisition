"""Gemini (Google AI Studio) wrapper.

One place to build the client and make a JSON-returning call so callers don't
re-implement it. Uses the free-tier Gemini API — set GEMINI_API_KEY (get one at
https://aistudio.google.com/apikey).

We ask Gemini for `application/json`, so it returns a bare JSON object (no
markdown fences) that we can json.loads directly. Thinking is disabled so the
whole token budget goes to the answer.

The free tier is rate-limited (~5 requests/minute), so a burst of drafts will
get 429 RESOURCE_EXHAUSTED. generate_json retries those, honoring the server's
suggested retry delay, so all drafts in a batch eventually succeed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from functools import lru_cache

from google import genai
from google.genai import types

from .env import env_float

log = logging.getLogger(__name__)

MAX_RETRIES = 6
DEFAULT_BACKOFF_SECONDS = 30

# The free tier allows ~5 requests/minute. Firing as fast as we can and letting
# the retry path absorb the 429s is far slower than it looks: every rejection
# costs a full 60s backoff, so a 15-draft batch spent ~18 minutes mostly asleep
# and still lost 11 drafts to exhausted retries (after their Apollo credits had
# already been spent). Pacing the calls ~12s apart keeps us under the limit, so
# the same batch takes ~3 minutes and rarely 429s at all.
MIN_INTERVAL_SECONDS = env_float("GEMINI_MIN_INTERVAL_SECONDS", 12.5)
_last_call_at: float = 0.0


def _pace() -> None:
    """Block until enough time has passed since the previous request."""
    global _last_call_at
    if MIN_INTERVAL_SECONDS > 0:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
    _last_call_at = time.monotonic()


def _thinking_config(model: str) -> types.ThinkingConfig:
    """Turn thinking down as far as the model family allows.

    The 2.x models take a token budget (`thinking_budget=0` switches thinking
    off). The 3.x models replaced that with `thinking_level` and reject a budget
    outright with 400 INVALID_ARGUMENT, so the control has to be picked per
    family or every 3.x call fails.
    """
    if re.match(r"gemini-([3-9]|\d{2,})", model):
        return types.ThinkingConfig(thinking_level="LOW")
    return types.ThinkingConfig(thinking_budget=0)


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def _retry_wait(err_text: str, attempt: int) -> float:
    """Seconds to wait before retrying. Prefer the server's retryDelay, else
    back off (the free tier resets its per-minute quota within ~60s)."""
    m = re.search(r"retry(?:Delay'?:?\s*'?|\s+in\s+)(\d+(?:\.\d+)?)s", err_text, re.IGNORECASE)
    if m:
        return float(m.group(1)) + 2
    return min(60, DEFAULT_BACKOFF_SECONDS * (attempt + 1))


def generate_json(
    *,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
) -> dict:
    """Call Gemini and parse its reply as a JSON object, retrying on rate limits."""
    config = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
        response_mime_type="application/json",
        thinking_config=_thinking_config(model),
    )
    for attempt in range(MAX_RETRIES):
        try:
            _pace()
            resp = _client().models.generate_content(
                model=model, contents=prompt, config=config
            )
            return json.loads(resp.text)
        except Exception as exc:
            msg = str(exc)
            transient = any(s in msg for s in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"))
            if not transient or attempt == MAX_RETRIES - 1:
                raise
            wait = _retry_wait(msg, attempt)
            log.warning("Gemini rate-limited; retrying in %.0fs (attempt %d/%d)",
                        wait, attempt + 1, MAX_RETRIES)
            time.sleep(wait)
    raise RuntimeError("generate_json exhausted retries")  # unreachable
