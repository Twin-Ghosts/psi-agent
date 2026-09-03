"""Itemised accounting for the assembled system prompt.

The kernel logs one number for the whole prompt (``System prompt loaded``
/ ``System prompt rebuilt`` in ``system_prompt.py``). That number is the
fixed cost re-sent on **every** turn, so when it grows to ~210k chars the
only useful question is *which section* is paying for it — and a single
total cannot answer that.

This module is the answer. A workspace builder appends its prompt through
:class:`PromptBudget` instead of a bare ``list[str]``, and the resulting
:meth:`PromptBudget.render` output *is* the prompt — so the itemisation is
derived from the same list that produced the string, not from a parallel
tally that can silently drift out of sync with it.

Two deliberate choices, both learned the hard way in this repo:

* **Reconciliation is by construction, not by convention.** ``render()``
  joins exactly what ``breakdown()`` measures, and the join separators are
  their own line item. :attr:`PromptBreakdown.residual` is therefore
  always 0; it is reported anyway, and logged at WARNING when it is not,
  because a nonzero residual means this module has a bug and the numbers
  underneath it cannot be trusted.
* **The breakdown logs at INFO.** Production pins INFO (see
  ``_logging.setup_logging``), and this repo has already shipped the
  mistake of putting the one number worth having behind DEBUG. It logs
  through loguru rather than stdlib ``logging`` on purpose: nothing in
  this project configures the stdlib root logger, so a stdlib
  ``logger.info`` from a workspace module is discarded before it reaches
  any sink.

Tool JSON schemas are **not** part of the prompt — they travel in the
request body's own ``tools`` array (see ``agent.py``). They are a real
per-turn fixed cost, but they do not belong in this total and are
reported separately by :func:`log_tool_schema_size`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# Sections below this share of the total are folded into one "other" line so
# the log stays one screen. They are still counted in full.
_SMALL_ITEM_SHARE = 0.005

_SEPARATOR_LABEL = "join separators (\\n)"


@dataclass(frozen=True)
class PromptItem:
    """One labelled contributor to the prompt, with its char count."""

    label: str
    chars: int
    # How many appended fragments carry this label — a section built from
    # several ``add`` calls (per-file bootstrap, say) is still one item.
    fragments: int = 1

    def share_of(self, total: int) -> float:
        return (self.chars / total) if total else 0.0


@dataclass(frozen=True)
class PromptBreakdown:
    """Itemised prompt cost. ``residual`` is 0 unless this module is wrong."""

    total: int
    items: tuple[PromptItem, ...]
    separators: int
    residual: int

    @property
    def accounted(self) -> int:
        return sum(item.chars for item in self.items) + self.separators

    def reconciles(self) -> bool:
        return self.residual == 0


@dataclass
class PromptBudget:
    """Collects labelled prompt fragments, renders them, and itemises them.

    Use it exactly where a ``list[str]`` of prompt parts would go::

        budget = PromptBudget()
        budget.add("identity (SOUL.md)", identity)
        budget.add("static: tool call style", "", TOOL_CALL_STYLE_SECTION)
        prompt = budget.render()
        budget.log(context="agent=feishu")

    Empty and blank fragments are kept, not dropped: the assembled prompt
    contains them, so dropping them here would break reconciliation.
    """

    separator: str = "\n"
    _parts: list[tuple[str, str]] = field(default_factory=list)

    def add(self, label: str, *texts: str) -> None:
        """Append one or more fragments, all attributed to ``label``.

        Passing several fragments mirrors the ``["", SECTION]`` idiom of the
        workspace builders: the blank spacer is charged to the section it
        introduces rather than to a catch-all.
        """
        for text in texts:
            self._parts.append((label, text))

    def add_if(self, condition: object, label: str, *texts: str | None) -> bool:
        """``add`` when ``condition`` is truthy. Returns whether it added.

        Conditionally-injected sections are the interesting ones for a
        trimming decision — this keeps the call site a single line so the
        condition stays readable next to the label.

        ``texts`` admits ``None`` because the usual call site passes the very
        value it just tested (``add_if(x := f(), label, "", x)``) and the
        builders that feed it return ``str | None``. A ``None`` only ever
        arrives on the falsy path, which returns before reaching ``add``.
        """
        if not condition:
            return False
        self.add(label, *(text for text in texts if text is not None))
        return True

    def render(self) -> str:
        """The assembled prompt. This is what ``breakdown()`` measures."""
        return self.separator.join(text for _, text in self._parts)

    def breakdown(self, actual: str | None = None) -> PromptBreakdown:
        """Itemise by label, preserving first-appearance order.

        ``actual`` is the prompt string as finally handed to the kernel. Pass
        it whenever the caller post-processes ``render()`` output — the whole
        point of the residual is to catch text that reached the model without
        passing through a labelled ``add``, and reconciling against our own
        ``render()`` could never catch that.
        """
        chars: dict[str, int] = {}
        fragments: dict[str, int] = {}
        for label, text in self._parts:
            chars[label] = chars.get(label, 0) + len(text)
            fragments[label] = fragments.get(label, 0) + 1

        items = tuple(PromptItem(label, chars[label], fragments[label]) for label in chars)
        separators = len(self.separator) * max(len(self._parts) - 1, 0)
        total = len(self.render() if actual is None else actual)
        return PromptBreakdown(
            total=total,
            items=items,
            separators=separators,
            residual=total - sum(item.chars for item in items) - separators,
        )

    def log(self, *, context: str = "", actual: str | None = None) -> PromptBreakdown:
        """Log the itemised breakdown at INFO and return it."""
        breakdown = self.breakdown(actual)
        log_breakdown(breakdown, context=context)
        return breakdown


def log_breakdown(breakdown: PromptBreakdown, *, context: str = "") -> None:
    """Emit the itemisation at INFO, largest item first.

    One line per section. Small sections are summarised on a single line so
    that 150-odd tools' worth of one-line entries cannot bury the sections
    that actually matter — their chars stay in the total either way.
    """
    suffix = f" [{context}]" if context else ""
    total = breakdown.total
    logger.info(f"System prompt breakdown{suffix}: total={total} chars (~{total // 4} tokens)")

    ranked = sorted(breakdown.items, key=lambda item: item.chars, reverse=True)
    folded: list[PromptItem] = []
    for item in ranked:
        if item.share_of(total) < _SMALL_ITEM_SHARE and item.chars < total:
            folded.append(item)
            continue
        fragments = f" x{item.fragments}" if item.fragments > 1 else ""
        logger.info(f"  {item.chars:>7} chars {item.share_of(total) * 100:>5.1f}%  {item.label}{fragments}")

    if folded:
        small = sum(item.chars for item in folded)
        share = (small / total * 100) if total else 0.0
        logger.info(f"  {small:>7} chars {share:>5.1f}%  <{_SMALL_ITEM_SHARE * 100:g}% each, {len(folded)} sections")

    if breakdown.separators:
        share = breakdown.separators / total * 100 if total else 0.0
        logger.info(f"  {breakdown.separators:>7} chars {share:>5.1f}%  {_SEPARATOR_LABEL}")

    # Stated unconditionally: "the parts add up" is the claim that makes every
    # number above worth acting on, so it is on the record next to them.
    if breakdown.reconciles():
        logger.info(f"  reconciled: {breakdown.accounted} accounted == {total} total, residual 0")
    else:
        logger.warning(
            f"System prompt breakdown does NOT reconcile{suffix}: "
            f"{breakdown.accounted} accounted vs {total} total, "
            f"residual {breakdown.residual:+d} chars — itemisation is unreliable, do not size a trim from it"
        )


def log_tool_schema_size(tool_defs: list[dict[str, Any]], *, context: str = "") -> int:
    """Log the serialised size of the request's ``tools`` array at INFO.

    Deliberately *not* folded into the prompt breakdown: these schemas are a
    sibling field of ``messages`` in the request body, not prompt text, so
    adding them to the prompt total would produce a number that matches
    neither the ``System prompt loaded`` log nor the request. They are logged
    because they are the same kind of cost — paid in full on every turn — and
    a trimming decision needs both figures side by side.

    Returns the serialised char count.
    """
    if not tool_defs:
        return 0
    # ``separators`` matches the compact form an HTTP client actually sends;
    # the default ``json.dumps`` spacing would overstate this by ~2 chars/key.
    payload = json.dumps(tool_defs, ensure_ascii=False, separators=(",", ":"))
    chars = len(payload)
    suffix = f" [{context}]" if context else ""
    logger.info(
        f"Tool schemas{suffix}: {len(tool_defs)} tools, {chars} chars (~{chars // 4} tokens) "
        f"in the request tools array — separate from the system prompt total"
    )
    return chars
