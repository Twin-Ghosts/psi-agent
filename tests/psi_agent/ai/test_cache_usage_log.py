"""Prompt-cache observability.

The session keeps the request prefix byte-stable so that a cached prefix *can*
be reused, but caching is opt-in upstream and nothing in ``src/`` requests it
yet. Without a log line reporting ``cached_tokens`` there is no way to tell
which of those two states we are in, so these tests pin that the counter is
surfaced when the provider reports one and stays quiet when it does not.
"""

from __future__ import annotations

from typing import Any

import pytest
from loguru import logger

from psi_agent.ai.server import _log_cache_usage


class _Details:
    def __init__(self, cached_tokens: int) -> None:
        self.cached_tokens = cached_tokens


class _Usage:
    def __init__(self, prompt_tokens: int = 0, details: _Details | None = None) -> None:
        self.prompt_tokens = prompt_tokens
        if details is not None:
            self.prompt_tokens_details = details


@pytest.fixture
def logged() -> Any:
    """Collect loguru INFO records emitted during the test."""
    records: list[str] = []
    sink_id = logger.add(lambda msg: records.append(msg.record["message"]), level="INFO")
    yield records
    logger.remove(sink_id)


def test_cache_hit_is_reported_with_share(logged: list[str]) -> None:
    _log_cache_usage(_Usage(prompt_tokens=1000, details=_Details(cached_tokens=800)))

    assert logged == ["Prompt cache: cached_tokens=800, prompt_tokens=1000, 80%"]


def test_zero_cached_tokens_is_still_reported(logged: list[str]) -> None:
    """A zero is the signal that caching is off — it must not be swallowed."""
    _log_cache_usage(_Usage(prompt_tokens=1000, details=_Details(cached_tokens=0)))

    assert logged == ["Prompt cache: cached_tokens=0, prompt_tokens=1000, 0%"]


def test_provider_without_cache_detail_stays_quiet(logged: list[str]) -> None:
    """No detail means no information, which is not the same as a zero hit."""
    _log_cache_usage(_Usage(prompt_tokens=1000))

    assert logged == []


def test_none_detail_stays_quiet(logged: list[str]) -> None:
    usage = _Usage(prompt_tokens=1000)
    usage.prompt_tokens_details = None  # type: ignore[assignment]

    _log_cache_usage(usage)

    assert logged == []


def test_zero_prompt_tokens_does_not_divide_by_zero(logged: list[str]) -> None:
    """Heartbeat-ish chunks can report zero prompt tokens; the share is dropped."""
    _log_cache_usage(_Usage(prompt_tokens=0, details=_Details(cached_tokens=0)))

    assert logged == ["Prompt cache: cached_tokens=0, prompt_tokens=0"]
