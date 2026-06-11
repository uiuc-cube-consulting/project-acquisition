"""Gemini (Google AI Studio) wrapper.

One place to build the client and make a JSON-returning call, so the drafter,
reply classifier, and approval parser don't each re-implement it. Uses the
free-tier Gemini API — set GEMINI_API_KEY (get one at
https://aistudio.google.com/apikey).

We ask Gemini for `application/json`, so it returns a bare JSON object (no
markdown fences) that we can json.loads directly. Thinking is disabled so the
whole token budget goes to the answer and short calls stay fast and cheap.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

from google import genai
from google.genai import types


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def generate_json(
    *,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
) -> dict:
    """Call Gemini and parse its reply as a JSON object."""
    resp = _client().models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return json.loads(resp.text)
