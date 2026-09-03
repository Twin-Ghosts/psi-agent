"""The prompt breakdown must add up, and must be visible at INFO.

Both properties are the whole point of ``prompt_budget``: a breakdown that
does not reconcile cannot size a trim, and one that only logs at DEBUG is
invisible in production (which pins INFO).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
from loguru import logger

from psi_agent.session.prompt_budget import PromptBudget, log_tool_schema_size


@pytest.fixture
def sink() -> Iterator[list[tuple[str, str]]]:
    """Capture loguru records as ``(level, message)`` at INFO and above."""
    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="INFO",
        format="{message}",
    )
    yield records
    logger.remove(handler_id)


def test_render_is_the_joined_parts() -> None:
    budget = PromptBudget()
    budget.add("a", "alpha")
    budget.add("b", "", "beta")
    assert budget.render() == "alpha\n\nbeta"


def test_breakdown_reconciles_with_total() -> None:
    budget = PromptBudget()
    budget.add("identity", "x" * 100)
    budget.add("tools", "", "y" * 250)
    budget.add("memory", "", "z" * 40)

    breakdown = budget.breakdown()

    assert breakdown.total == len(budget.render())
    assert breakdown.residual == 0
    assert breakdown.reconciles()
    # The sum is load-bearing, so assert it directly rather than trusting
    # ``reconciles()`` to be implemented correctly.
    assert sum(i.chars for i in breakdown.items) + breakdown.separators == breakdown.total


def test_breakdown_groups_fragments_under_one_label() -> None:
    budget = PromptBudget()
    budget.add("section", "", "body")
    budget.add("section", "", "more")

    (item,) = budget.breakdown().items
    assert item.label == "section"
    assert item.fragments == 4
    assert item.chars == len("body") + len("more")


def test_empty_fragments_are_counted_not_dropped() -> None:
    """Blank spacers are real chars in the prompt; dropping them breaks the sum."""
    budget = PromptBudget()
    budget.add("a", "", "")
    breakdown = budget.breakdown()
    assert breakdown.separators == 1
    assert breakdown.residual == 0


def test_residual_detects_text_that_bypassed_the_budget() -> None:
    """A post-processed prompt must show up as a nonzero residual.

    This is the mutation check for the reconciliation claim: if ``breakdown``
    quietly measured its own ``render()`` instead of the real string, this
    would pass with residual 0 and the guarantee would be worthless.
    """
    budget = PromptBudget()
    budget.add("a", "alpha")

    spliced = budget.render() + "\nSMUGGLED"
    breakdown = budget.breakdown(spliced)

    assert not breakdown.reconciles()
    assert breakdown.residual == len("\nSMUGGLED")


def test_add_if_skips_falsy_and_reports() -> None:
    budget = PromptBudget()
    assert budget.add_if(False, "skipped", "", "nope") is False
    assert budget.add_if("", "skipped-empty", "", "nope") is False
    assert budget.add_if(True, "kept", "yes") is True
    assert budget.render() == "yes"
    assert [i.label for i in budget.breakdown().items] == ["kept"]


def test_breakdown_logs_at_info(sink: list[tuple[str, str]]) -> None:
    """Production pins INFO — the numbers must not hide behind DEBUG."""
    budget = PromptBudget()
    budget.add("big section", "x" * 5000)
    budget.add("small section", "", "y" * 5)

    budget.log(context="agent=test")

    messages = [m for level, m in sink if level == "INFO"]
    assert any("System prompt breakdown [agent=test]" in m for m in messages)
    assert any("big section" in m for m in messages)
    assert any("reconciled" in m and "residual 0" in m for m in messages)


def test_nonreconciling_breakdown_logs_a_warning(sink: list[tuple[str, str]]) -> None:
    budget = PromptBudget()
    budget.add("a", "alpha")

    budget.log(actual=budget.render() + "EXTRA")

    warnings = [m for level, m in sink if level == "WARNING"]
    assert warnings, "a residual must be loud, not folded into an 'other' line"
    assert "does NOT reconcile" in warnings[0]
    assert "+5" in warnings[0]


def test_small_sections_are_folded_but_still_counted(sink: list[tuple[str, str]]) -> None:
    budget = PromptBudget()
    budget.add("dominant", "x" * 10_000)
    for index in range(6):
        budget.add(f"tiny-{index}", "y" * 4)

    breakdown = budget.log()

    assert breakdown.reconciles()
    messages = [m for level, m in sink if level == "INFO"]
    assert any("6 sections" in m for m in messages)
    # Folded lines must not be itemised individually.
    assert not any("tiny-3" in m for m in messages)


def test_tool_schema_size_is_logged_separately_at_info(sink: list[tuple[str, str]]) -> None:
    """Schemas ride in the request ``tools`` field, not the prompt."""
    tool_defs = [
        {
            "type": "function",
            "function": {"name": "read", "description": "Read a file", "parameters": {"type": "object"}},
        }
    ]

    chars = log_tool_schema_size(tool_defs, context="pack=test")

    assert chars == len(json.dumps(tool_defs, ensure_ascii=False, separators=(",", ":")))
    messages = [m for level, m in sink if level == "INFO"]
    assert any("Tool schemas [pack=test]" in m and "1 tools" in m for m in messages)
    assert any("separate from the system prompt total" in m for m in messages)


def test_tool_schema_size_of_no_tools_is_zero(sink: list[tuple[str, str]]) -> None:
    assert log_tool_schema_size([]) == 0
    assert not [m for level, m in sink if "Tool schemas" in m]


def test_zero_length_prompt_does_not_divide_by_zero(sink: list[tuple[str, str]]) -> None:
    breakdown = PromptBudget().log()
    assert breakdown.total == 0
    assert breakdown.reconciles()


def test_breakdown_does_not_log_through_stdlib_logging(caplog: pytest.LogCaptureFixture) -> None:
    """Guard the delivery path, not just the level.

    Nothing in this project configures the stdlib root logger, so a stdlib
    ``INFO`` record is discarded before reaching any sink. Measured: a
    ``logging.getLogger(...).info(...)`` call after ``setup_logging()``
    produces no output at all, while WARNING escapes via ``lastResort``.
    """
    with caplog.at_level(logging.DEBUG):
        PromptBudget().log()
    assert not [r for r in caplog.records if "breakdown" in r.getMessage()]
