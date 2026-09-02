"""Writers must land deliverables in the session workspace — never the agent package.

Production incident: 海豚二号 generated a TODO 拆解报告 with ``write_excel`` and a
bare output name ("todo颗粒度检查与拆解_0902.xlsx"). The tool resolved it against
the tool process cwd — the agent package directory (agents/feishu) — while the
model's ``[SEND:…]`` marker pointed at the session workspace, so the file was
"written successfully", never delivered, and the version-controlled package tree
gained a stray .xlsx. ``write_word`` had the same latent defect.

These tests pin the fix: bare/relative output paths resolve under the session
workspace (``_runtime_paths.resolve_user_path``), the success message reports the
resolved path, and absolute paths into the agent package are refused outright
(the runtime binds workspace and agent apart; unbound single-root fallback is
not refused).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from psi_agent.session import runtime_context

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
AGENT_DIR = TOOLS_DIR.parent  # agents/feishu — version-controlled, must stay clean
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

excel = importlib.import_module("write_excel")
word = importlib.import_module("write_word")

pytestmark = pytest.mark.asyncio

ROWS = '[["Name", "Score"], ["Alice", 92], ["Bob", 88]]'
BLOCKS = [{"type": "paragraph", "text": "报告正文"}]


async def _in_session(ws: Path, call):
    """Run one tool call exactly as a Session turn does: workspace and agent
    package bound apart on the runtime ContextVars."""
    with runtime_context.path_scope(workspace=str(ws), agent=str(AGENT_DIR)):
        return await call()


async def test_write_excel_bare_name_lands_in_workspace(tmp_path) -> None:
    result = await _in_session(tmp_path, lambda: excel.write_excel("report.xlsx", ROWS))
    assert "[OK]" in result, result
    assert str(tmp_path) in result, "success message must report the resolved workspace path"
    assert (tmp_path / "report.xlsx").is_file()


async def test_write_excel_adds_extension_then_resolves(tmp_path) -> None:
    await _in_session(tmp_path, lambda: excel.write_excel("report", ROWS))
    assert (tmp_path / "report.xlsx").is_file()


async def test_write_excel_relative_subdir_resolves_under_workspace(tmp_path) -> None:
    await _in_session(tmp_path, lambda: excel.write_excel("out/report.xlsx", ROWS))
    assert (tmp_path / "out" / "report.xlsx").is_file()


async def test_write_excel_absolute_path_outside_package_used_as_is(tmp_path) -> None:
    outside = tmp_path / "elsewhere" / "r.xlsx"
    result = await _in_session(tmp_path, lambda: excel.write_excel(str(outside), ROWS))
    assert "[OK]" in result, result
    assert outside.is_file()


async def test_write_excel_refuses_agent_package_dir(tmp_path) -> None:
    """The exact incident: an absolute path inside agents/feishu must not be written."""
    poisoned = AGENT_DIR / "stray-output.xlsx"
    result = await _in_session(tmp_path, lambda: excel.write_excel(str(poisoned), ROWS))
    assert "[Error]" in result and "agent package" in result, result
    assert not poisoned.exists(), "the package dir must stay clean"


async def test_write_word_bare_name_lands_in_workspace(tmp_path) -> None:
    result = await _in_session(tmp_path, lambda: word.write_word("report.docx", BLOCKS))
    assert "[OK]" in result, result
    assert str(tmp_path) in result, "success message must report the resolved workspace path"
    assert (tmp_path / "report.docx").is_file()


async def test_write_word_relative_subdir_resolves_under_workspace(tmp_path) -> None:
    await _in_session(tmp_path, lambda: word.write_word("docs/report.docx", BLOCKS))
    assert (tmp_path / "docs" / "report.docx").is_file()


async def test_write_word_refuses_agent_package_dir(tmp_path) -> None:
    poisoned = AGENT_DIR / "stray-output.docx"
    result = await _in_session(tmp_path, lambda: word.write_word(str(poisoned), BLOCKS))
    assert "[Error]" in result and "agent package" in result, result
    assert not poisoned.exists()


async def test_write_word_from_markdown_resolves_source_and_output(tmp_path) -> None:
    (tmp_path / "draft.md").write_text("# 标题\n\n正文段落", encoding="utf-8")

    async def run() -> str:
        return await word.write_word_from_markdown("draft.md", "final.docx")

    result = await _in_session(tmp_path, run)
    assert "[OK]" in result, result
    assert (tmp_path / "final.docx").is_file()
