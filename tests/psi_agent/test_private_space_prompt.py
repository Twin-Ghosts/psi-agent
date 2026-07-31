"""隔离约束进不进系统提示词 —— 按 ``enabled()`` 条件拼接。

守卫本体挡得住路径工具, 但 shell 只能启发式扫描, 挡不住变量拼接 / base64 / 中转文件。
`PRIVATE_SPACE_SECTION` 是对那道缺口的**软约束**(要求 agent 拒绝协助绕过), 故必须真的
出现在开启隔离时的提示词里, 且在未开启时不占 token。

`examples/*/systems/system.py` 靠裸名 import 同目录的 `prompt_sections`, 多个 workspace
各有一份同名文件, 所以按 `test_compact_history_chaining` 的做法手工装载并隔离 `sys.modules`。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SYSTEM_PY = Path("examples/haitun-workspace/systems/system.py")
_SIBLING_MODULES = ("prompt_sections", "prompt_texts", "tool_docs")


def _load(path: Path) -> Any:
    name = f"promptmod_{path.parent.parent.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    saved = {k: sys.modules.pop(k) for k in _SIBLING_MODULES if k in sys.modules}
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
        for k in _SIBLING_MODULES:
            sys.modules.pop(k, None)
        sys.modules.update(saved)
    return module


@pytest.fixture
def sysmod() -> Any:
    return _load(_SYSTEM_PY)


def test_section_states_the_rule_and_the_evasions(sysmod: Any) -> None:
    """光说"别看别人的"不够, 得点名那几种绕法, 否则模型会觉得换个手段就不算越界。"""
    text = sysmod.PRIVATE_SPACE_SECTION
    assert "public/" in text
    for evasion in ("base64", "variables", "copying", "bash"):
        assert evasion in text, f"未点名绕法: {evasion}"
    # 被拒时的正确反应: 如实说, 不换路重试。
    assert "not a broken tool" in text
    assert "do not retry by another route" in text


def test_section_gated_on_enabled(sysmod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """未开隔离时不拼进去 —— 没有边界却宣称有, 比不说更糟。"""
    monkeypatch.delenv("PSI_WORKSPACE_ROOT", raising=False)
    assert sysmod._private_space_enabled() is False
    monkeypatch.setenv("PSI_WORKSPACE_ROOT", "/tmp/ws-root")
    assert sysmod._private_space_enabled() is True


@pytest.mark.anyio
async def test_prompt_contains_section_only_when_enabled(
    sysmod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端: 同一个 workspace, 只切环境变量, 看整段有没有进最终提示词。"""
    anchor = "## Per-user file isolation"

    monkeypatch.delenv("PSI_WORKSPACE_ROOT", raising=False)
    off = await sysmod.system_prompt_builder(workspace_raw=str(tmp_path))
    assert anchor not in off

    monkeypatch.setenv("PSI_WORKSPACE_ROOT", str(tmp_path.parent))
    on = await sysmod.system_prompt_builder(workspace_raw=str(tmp_path))
    assert anchor in on
    # 拼的是整段而不是标题行。
    assert "do not retry by another route" in on
