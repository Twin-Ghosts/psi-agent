"""Reconcile a real agent pack's system prompt against its own PromptBudget.

``tests/psi_agent/session/test_prompt_budget.py`` exercises ``PromptBudget`` in
isolation, so its residual cases stay green even when a *builder* post-processes
``render()`` output and forgets to charge the difference. This runs the genuine
``system_prompt_builder`` for an agent pack and reports the residual it actually
produces, by intercepting the ``budget.log(actual=...)`` call the builder makes.

Usage::

    PYTHONPATH=src python scripts/check_prompt_residual.py feishu desktop

Exits non-zero if any pack fails to reconcile.
"""

# ruff: noqa: T201  这是命令行脚本, stdout 就是它的输出通道。

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from psi_agent.session.prompt_budget import PromptBreakdown, PromptBudget  # noqa: E402


def _load_pack_system(pack: str) -> Any:
    workspace = REPO_ROOT / "agents" / pack
    sys.path.insert(0, str(workspace / "systems"))
    sys.path.insert(0, str(workspace / "tools"))
    spec = importlib.util.spec_from_file_location(f"residual_{pack}_system", workspace / "systems" / "system.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _report(pack: str) -> bool:
    module = _load_pack_system(pack)
    captured: list[PromptBreakdown] = []
    real_log = PromptBudget.log

    def spy_log(self: PromptBudget, *, context: str = "", actual: str | None = None) -> PromptBreakdown:
        breakdown = real_log(self, context=context, actual=actual)
        captured.append(breakdown)
        return breakdown

    PromptBudget.log = spy_log  # type: ignore[method-assign]
    try:
        # A message that trips the profile path, so profile/policy are non-empty.
        message = {"role": "user", "content": "深入讲 Python 原理", "user_id": "residual-probe"}
        with tempfile.TemporaryDirectory() as tmp:
            prompt = await module.system_prompt_builder(message, workspace_raw=tmp)
            prompt_breakdown = captured[-1] if captured else None
            # The volatile blocks moved here, so their accounting has to be
            # reconciled too — a prompt that balances on its own says nothing
            # about text that now reaches the model by another route.
            #
            # Called both ways on purpose: the pre-move builder takes no message,
            # and being able to measure both shapes is what makes a before/after
            # comparison possible at all.
            try:
                block = await module.turn_context_builder(message, workspace_raw=tmp)
            except TypeError:
                block = await module.turn_context_builder()
            turn_breakdown = captured[-1] if len(captured) > 1 else None
    finally:
        PromptBudget.log = real_log  # type: ignore[method-assign]

    ok = True
    for name, breakdown, rendered in (
        ("prompt", prompt_breakdown, prompt),
        ("turn-context", turn_breakdown, block),
    ):
        if breakdown is None:
            print(f"[{pack}] {name} FAIL: builder never called budget.log — nothing reconciled")
            ok = False
            continue
        status = "OK" if breakdown.reconciles() else "FAIL"
        ok = ok and breakdown.reconciles()
        print(
            f"[{pack}] {name} {status}: rendered={len(rendered)} chars, total={breakdown.total}, "
            f"residual={breakdown.residual:+d}, {len(breakdown.items)} items"
        )
    return ok


def _run_each_pack_in_its_own_process(packs: list[str]) -> int:
    """One subprocess per pack. Measuring both in one process cross-contaminates.

    ``agents/feishu/systems/`` and ``agents/desktop/systems/`` hold same-named
    private modules (``prompt_sections`` and friends). With both directories on
    ``sys.path``, whichever pack loads second gets the first one's copy —
    measured 2026-09-03: desktop reads 134169 chars alone and 135755 chars in a
    shared process, a +1586 difference that looks exactly like a residual
    regression.
    """
    failures = 0
    for pack in packs:
        completed = subprocess.run(
            [sys.executable, __file__, "--single", pack],
            check=False,
            cwd=str(REPO_ROOT),
        )
        failures += completed.returncode != 0
    return 1 if failures else 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv[:1] == ["--single"]:
        raise SystemExit(0 if asyncio.run(_report(argv[1])) else 1)
    raise SystemExit(_run_each_pack_in_its_own_process(argv or ["feishu", "desktop"]))
