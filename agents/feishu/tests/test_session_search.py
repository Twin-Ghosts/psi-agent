"""Tests for session keyword/task search tools."""

from __future__ import annotations

import builtins
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

from psi_agent.session.tool_registry import ToolFunction

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

session_helpers: Any = importlib.import_module("_session_helpers")
keyword_tool: Any = importlib.import_module("session_keyword_search")
task_tool: Any = importlib.import_module("session_task_search")


def test_keyword_tool_metadata() -> None:
    meta = ToolFunction.from_callable(keyword_tool.session_keyword_search)
    assert meta.name == "session_keyword_search"
    assert "query" in meta.parameters["properties"]


def test_task_tool_metadata() -> None:
    meta = ToolFunction.from_callable(task_tool.session_task_search)
    assert meta.name == "session_task_search"
    assert meta.parameters["required"] == ["category"]


@pytest.mark.anyio
async def test_keyword_search_finds_match(tmp_path: Path) -> None:
    histories = tmp_path / "histories"
    histories.mkdir()
    (histories / "alpha.jsonl").write_text(
        '{"role":"user","content":"talk about Docker deploy"}\n{"role":"assistant","content":"ok"}\n',
        encoding="utf-8",
    )
    (histories / "beta.jsonl").write_text(
        '{"role":"user","content":"unrelated"}\n',
        encoding="utf-8",
    )

    result = await session_helpers.keyword_search_sessions(
        query="Docker",
        workspace_raw=str(tmp_path),
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["hits"][0]["session_id"] == "alpha"
    assert result["hits"][0]["snippets"]


@pytest.mark.anyio
async def test_keyword_search_scoped_to_session(tmp_path: Path) -> None:
    histories = tmp_path / "histories"
    histories.mkdir()
    (histories / "one.jsonl").write_text('{"role":"user","content":"needle here"}\n', encoding="utf-8")
    (histories / "two.jsonl").write_text('{"role":"user","content":"needle there"}\n', encoding="utf-8")

    result = await session_helpers.keyword_search_sessions(
        query="needle",
        session_id="one",
        workspace_raw=str(tmp_path),
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["hits"][0]["session_id"] == "one"


@pytest.mark.anyio
async def test_keyword_search_empty_query(tmp_path: Path) -> None:
    result = await session_helpers.keyword_search_sessions(query="", workspace_raw=str(tmp_path))
    assert result["ok"] is False


@pytest.mark.anyio
async def test_task_search_subagent(tmp_path: Path) -> None:
    histories = tmp_path / "histories"
    histories.mkdir()
    (histories / "sub-abc12345.jsonl").write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")

    result = await session_helpers.task_search_sessions(
        category="subagent",
        workspace_raw=str(tmp_path),
        include_gateway=False,
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["hits"][0]["session_id"] == "sub-abc12345"
    assert "subagent" in result["hits"][0]["categories"]


@pytest.mark.anyio
async def test_task_search_github(tmp_path: Path) -> None:
    histories = tmp_path / "histories"
    histories.mkdir()
    (histories / "main.jsonl").write_text(
        '{"role":"user","content":"please open a GitHub pull request"}\n',
        encoding="utf-8",
    )

    result = await session_helpers.task_search_sessions(
        category="github",
        workspace_raw=str(tmp_path),
        include_gateway=False,
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert "github" in result["hits"][0]["categories"]


@pytest.mark.anyio
async def test_task_search_invalid_category(tmp_path: Path) -> None:
    result = await session_helpers.task_search_sessions(
        category="not-a-category",
        workspace_raw=str(tmp_path),
    )
    assert result["ok"] is False


def _history_line(role: str, content: str) -> str:
    return json.dumps({"role": role, "content": content}, ensure_ascii=False)


@pytest.mark.anyio
async def test_keyword_hit_reports_searchable_message_count_and_score(tmp_path: Path) -> None:
    """score = hit_count / searchable message_count, with tool/system lines excluded.

    ``message_count`` is computed lazily now; these exact numbers are what pin
    "lazy changed the timing, not the value".
    """
    histories = tmp_path / "histories"
    histories.mkdir()
    lines = [
        _history_line("user", "needle one"),
        _history_line("assistant", "needle two"),
        _history_line("assistant", "unrelated"),
        _history_line("user", "also unrelated"),
        # Neither of the next two is searchable, so neither may reach the score.
        _history_line("tool", "needle in a tool message"),
        _history_line("system", "needle in a system message"),
        "",
        "{malformed json",
    ]
    (histories / "alpha.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = await session_helpers.keyword_search_sessions(query="needle", workspace_raw=str(tmp_path))
    assert result["count"] == 1
    hit = result["hits"][0]
    assert hit["hit_count"] == 2
    assert hit["message_count"] == 4
    assert hit["score"] == pytest.approx(0.5)
    assert len(hit["snippets"]) == 2


@pytest.fixture
def opened_paths(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every path the helpers open for reading.

    The helpers read history files with the builtin ``open`` on a worker thread,
    so the builtin itself is what has to be wrapped.
    """
    seen: list[str] = []
    real_open = builtins.open

    def tracking_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    return seen


@pytest.mark.anyio
async def test_scoped_keyword_search_does_not_read_other_sessions(
    tmp_path: Path,
    opened_paths: list[str],
) -> None:
    """With session_id given, only that history file is opened."""
    histories = tmp_path / "histories"
    histories.mkdir()
    (histories / "wanted.jsonl").write_text(_history_line("user", "needle here") + "\n", encoding="utf-8")
    for i in range(5):
        (histories / f"other{i}.jsonl").write_text(_history_line("user", "needle there") + "\n", encoding="utf-8")

    result = await session_helpers.keyword_search_sessions(
        query="needle",
        session_id="wanted",
        workspace_raw=str(tmp_path),
    )

    assert result["count"] == 1
    assert result["hits"][0]["session_id"] == "wanted"
    assert [p for p in opened_paths if "other" in p] == []
    assert [p for p in opened_paths if "wanted.jsonl" in p]

    # The scan itself must be narrowed, not just the reads: an unscoped scan
    # would enumerate all six sessions before the search filtered them out.
    scoped = await session_helpers._scan_history_sessions(
        anyio.Path(str(tmp_path)),
        session_scope="wanted",
    )
    assert list(scoped) == ["wanted"]
    unscoped = await session_helpers._scan_history_sessions(anyio.Path(str(tmp_path)))
    assert len(unscoped) == 6


@pytest.mark.anyio
async def test_scoped_scan_of_missing_session_is_empty(tmp_path: Path) -> None:
    histories = tmp_path / "histories"
    histories.mkdir()
    (histories / "present.jsonl").write_text(_history_line("user", "hi") + "\n", encoding="utf-8")

    scoped = await session_helpers._scan_history_sessions(
        anyio.Path(str(tmp_path)),
        session_scope="absent",
    )
    assert scoped == {}


@pytest.mark.anyio
async def test_keyword_search_reads_each_history_once(tmp_path: Path, opened_paths: list[str]) -> None:
    """A keyword search must not read the corpus twice.

    The old code counted messages while listing and then re-read the same files
    to search them; one open per file is the regression guard.
    """
    histories = tmp_path / "histories"
    histories.mkdir()
    for i in range(4):
        (histories / f"s{i}.jsonl").write_text(_history_line("user", "needle") + "\n", encoding="utf-8")

    result = await session_helpers.keyword_search_sessions(query="needle", workspace_raw=str(tmp_path))

    assert result["count"] == 4
    for i in range(4):
        assert len([p for p in opened_paths if f"s{i}.jsonl" in p]) == 1


@pytest.mark.anyio
async def test_task_search_untitled_still_uses_message_count(tmp_path: Path) -> None:
    """The ``untitled`` rule needs message_count, which is no longer precomputed."""
    histories = tmp_path / "histories"
    histories.mkdir()
    (histories / "has-messages.jsonl").write_text(_history_line("user", "hi") + "\n", encoding="utf-8")
    # No role-bearing line: message_count == 0, so it is not "untitled".
    (histories / "empty-ish.jsonl").write_text('{"norole":1}\n\n{bad\n', encoding="utf-8")

    result = await session_helpers.task_search_sessions(
        category="untitled",
        workspace_raw=str(tmp_path),
        include_gateway=False,
    )
    ids = [h["session_id"] for h in result["hits"]]
    assert ids == ["has-messages"]
    assert result["hits"][0]["message_count"] == 1


@pytest.mark.anyio
async def test_tools_return_json(tmp_path: Path) -> None:
    histories = tmp_path / "histories"
    histories.mkdir()
    (histories / "x.jsonl").write_text('{"role":"user","content":"findme"}\n', encoding="utf-8")

    kw = json.loads(await keyword_tool.session_keyword_search(query="findme", workspace=str(tmp_path)))
    task = json.loads(
        await task_tool.session_task_search(category="all", workspace=str(tmp_path), include_gateway=False)
    )
    assert kw["ok"] is True
    assert task["ok"] is True
    assert task["count"] == 1
