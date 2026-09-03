"""The ``tools`` array is assembled once per Session and then reused verbatim.

``tools`` participates in the upstream prefix-cache key: 2026-09-03 measurement
against deepseek-v4-flash showed that dropping a single tool from an 8-tool
array cut the cache hit from 19456 to 13568 tokens, though the tool region was
0.7% of the body. So an array that grows as new tools appear pays a full
re-prefill each time it changes.

Tools are reloaded from disk every turn (``ToolRegistry.refresh``), which means
the array is *not* naturally stable: adding a tool file mid-Session, or a
registry that reports its tools in a different order, both rewrite it. These
tests pin that the array a Session sends stays byte-identical for that
Session's life, and that freezing is per Session rather than global.
"""

from __future__ import annotations

import json

from psi_agent.session.tool_defs import ToolDefsCache, build_tool_defs


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"desc for {name}"
        self.parameters = {"type": "object", "properties": {}}


def _registry(*names: str) -> dict[str, _FakeTool]:
    return {name: _FakeTool(name) for name in names}


def test_build_tool_defs_shapes_the_openai_wire_form() -> None:
    defs = build_tool_defs(_registry("alpha"))

    assert defs == [
        {
            "type": "function",
            "function": {
                "name": "alpha",
                "description": "desc for alpha",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_new_tool_appearing_mid_session_does_not_change_the_array() -> None:
    """A tool file dropped into the workspace must not re-prefill the session."""
    cache = ToolDefsCache()
    first = cache.freeze(build_tool_defs(_registry("alpha", "beta")))

    second = cache.freeze(build_tool_defs(_registry("alpha", "beta", "gamma")))

    assert second == first
    assert [d["function"]["name"] for d in second] == ["alpha", "beta"]


def test_frozen_array_is_byte_identical_across_turns() -> None:
    """The cache key is bytes, so compare serialised bytes, not just equality."""
    cache = ToolDefsCache()
    first = json.dumps(cache.freeze(build_tool_defs(_registry("alpha", "beta"))), ensure_ascii=False)

    for extra in ("gamma", "delta", "epsilon"):
        cache.freeze(build_tool_defs(_registry("alpha", "beta", extra)))
    last = json.dumps(cache.freeze(build_tool_defs(_registry("alpha", "beta"))), ensure_ascii=False)

    assert last == first


def test_tool_count_is_not_reduced_by_freezing() -> None:
    """Freezing is a stability measure, not a trim — the first array is sent whole."""
    cache = ToolDefsCache()

    frozen = cache.freeze(build_tool_defs(_registry(*(f"tool_{i:03d}" for i in range(210)))))

    assert len(frozen) == 210


def test_a_changed_description_does_not_leak_into_a_frozen_array() -> None:
    """Editing a tool's docstring mid-session is still a prefix change."""
    cache = ToolDefsCache()
    cache.freeze(build_tool_defs(_registry("alpha")))

    edited = _FakeTool("alpha")
    edited.description = "rewritten description"
    frozen = cache.freeze(build_tool_defs({"alpha": edited}))

    assert frozen[0]["function"]["description"] == "desc for alpha"


def test_registry_reordering_does_not_change_the_array() -> None:
    cache = ToolDefsCache()
    first = cache.freeze(build_tool_defs(_registry("alpha", "beta", "gamma")))

    reordered = cache.freeze(build_tool_defs(_registry("gamma", "alpha", "beta")))

    assert reordered == first


def test_each_session_freezes_its_own_array() -> None:
    """Freezing is per Session: a global cache would leak one pack into another."""
    one = ToolDefsCache()
    two = ToolDefsCache()

    one.freeze(build_tool_defs(_registry("alpha")))
    other = two.freeze(build_tool_defs(_registry("beta", "gamma")))

    assert [d["function"]["name"] for d in other] == ["beta", "gamma"]


def test_frozen_array_cannot_be_mutated_by_its_caller() -> None:
    """The request body is built from this; a caller pop() must not poison it."""
    cache = ToolDefsCache()
    handed_out = cache.freeze(build_tool_defs(_registry("alpha", "beta")))

    handed_out.pop()

    assert len(cache.freeze(build_tool_defs(_registry("alpha", "beta")))) == 2


def test_empty_registry_does_not_freeze_an_empty_array() -> None:
    """Tools load asynchronously; freezing "none yet" would disarm the session."""
    cache = ToolDefsCache()

    cache.freeze(build_tool_defs({}))
    later = cache.freeze(build_tool_defs(_registry("alpha")))

    assert [d["function"]["name"] for d in later] == ["alpha"]
