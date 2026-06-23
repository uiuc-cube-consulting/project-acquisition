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

log = logging.getLogger(__name__)

MAX_RETRIES = 6
DEFAULT_BACKOFF_SECONDS = 30


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
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    for attempt in range(MAX_RETRIES):
        try:
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
