"""Tests for the Gemini wrapper's per-family and retry logic.

Both functions here are pure, so nothing in this file calls the real API.
`_thinking_config` is the one that matters most for issue #13: switching the
drafting or classification model to another family is now a settings change, and
sending a 2.x thinking budget to a 3.x model is a hard 400.
"""
import pytest

from src.llm import DEFAULT_BACKOFF_SECONDS, _retry_wait, _thinking_config


@pytest.mark.parametrize(
    "model",
    [
        "gemini-2.5-flash",       # today's drafting model
        "gemini-2.0-flash",
        "gemini-2.5-flash-lite",
    ],
)
def test_thinking_config_2x_uses_budget(model):
    config = _thinking_config(model)
    assert config.thinking_budget == 0
    assert config.thinking_level is None


@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.5-flash-lite",  # today's classification model
        "gemini-3-pro",
        "gemini-9-flash",
        "gemini-10-pro",          # two-digit future version
        "gemini-12.5-flash",
    ],
)
def test_thinking_config_3x_and_later_uses_level(model):
    config = _thinking_config(model)
    assert config.thinking_level == "LOW"
    assert config.thinking_budget is None


@pytest.mark.parametrize(
    "err_text, expected",
    [
        # The shape the SDK raises: retryDelay inside the error payload.
        ("429 RESOURCE_EXHAUSTED {'retryDelay': '31s'}", 33.0),
        ("retryDelay: 7s", 9.0),
        ("Please retry in 12s", 14.0),
        ("retry in 0.5s", 2.5),
    ],
)
def test_retry_wait_prefers_server_delay(err_text, expected):
    assert _retry_wait(err_text, attempt=0) == expected


@pytest.mark.parametrize(
    "attempt, expected",
    [
        (0, DEFAULT_BACKOFF_SECONDS),
        (1, 60),
        (5, 60),  # capped, because the free tier's quota resets within ~60s
    ],
)
def test_retry_wait_backs_off_without_server_delay(attempt, expected):
    assert _retry_wait("429 RESOURCE_EXHAUSTED", attempt) == expected
