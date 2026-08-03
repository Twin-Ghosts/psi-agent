"""Tests for the shared event-shape helpers used by diagnostics and self-check."""

from __future__ import annotations

from psi_agent.channel._event_shapes import describe_shape, non_null_paths, plainify


class _UserId:
    def __init__(self, open_id: str) -> None:
        self.open_id = open_id
        self.union_id = None
        self._private = "hidden"


class _Message:
    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id
        self.message_id = "om_1"
        self.parent_id = None


class _Body:
    def __init__(self) -> None:
        self.message = _Message("oc_1")
        self.sender = _UserId("ou_1")


def test_plainify_unwraps_nested_objects() -> None:
    out = plainify(_Body())
    assert out == {
        "message": {"chat_id": "oc_1", "message_id": "om_1", "parent_id": None},
        "sender": {"open_id": "ou_1", "union_id": None},
    }


def test_plainify_drops_private_attrs() -> None:
    assert "_private" not in plainify(_UserId("ou_2"))


def test_plainify_survives_self_reference() -> None:
    class _Loop:
        def __init__(self) -> None:
            self.me = self

    assert isinstance(plainify(_Loop()), dict)


def test_plainify_passes_scalars_and_containers() -> None:
    assert plainify({"a": [1, "x", None]}) == {"a": [1, "x", None]}
    assert plainify(("t",)) == ["t"]


def test_describe_shape_reveals_nesting() -> None:
    shape = describe_shape(plainify(_Body()))
    # The whole point: chat_id is visibly *inside* message, not at top level.
    assert "message{" in shape
    assert "chat_id" in shape
    assert shape.index("message{") < shape.index("chat_id")


def test_describe_shape_marks_none_and_empty() -> None:
    assert describe_shape({"a": None}) == "a=None"
    assert describe_shape({}) == "{}"
    assert describe_shape([]) == "[]"


def test_non_null_paths_are_dotted_and_skip_empties() -> None:
    paths = non_null_paths(plainify(_Body()))
    assert "message.chat_id" in paths
    assert "sender.open_id" in paths
    # None / "" carry no value, so they are not offered as readable paths.
    assert "message.parent_id" not in paths
    assert "sender.union_id" not in paths


def test_non_null_paths_indexes_first_list_item() -> None:
    paths = non_null_paths({"users": [{"user_id": {"open_id": "ou_9"}}]})
    assert paths == ["users[0].user_id.open_id"]
