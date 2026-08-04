"""Isolate AppData root so history/todo dual-read does not touch the real user dir."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


def body_dict(request: Any) -> dict[str, Any]:
    """The request body as the dict Feishu will actually receive.

    Feishu tools build requests with the SDK's typed builders, so ``request.body`` is a
    model object rather than a mapping — comparing it to a literal dict would compare
    object identity and always fail. This serializes it the same way the transport does,
    which keeps assertions pinned to the wire format instead of to the SDK's in-memory
    representation.

    Plain dicts (hand-built requests for the endpoints the SDK doesn't model) pass
    through unchanged, so one helper covers both kinds of call site. ``None`` bodies
    read as ``{}`` so tests can assert on a key's absence without special-casing.
    """
    body = getattr(request, "body", None)
    if body is None:
        return {}
    if isinstance(body, dict):
        return body
    from lark_oapi.core.json import JSON  # noqa: PLC0415

    # ``marshal`` is typed as possibly returning None; an empty body reads as {} rather
    # than blowing up inside json.loads.
    raw = JSON.marshal(body)
    if not raw:
        return {}
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def body_field(request: Any, name: str) -> Any:
    """One field of the request body as the live Python object.

    Distinct from ``body_dict`` on purpose: upload tests must assert the binary is a real
    ``io.IOBase`` sitting in the body, because that is what makes the SDK send multipart
    instead of JSON. Serializing first would turn the stream into whatever JSON stands in
    for it and the assertion would no longer guard anything.
    """
    body = getattr(request, "body", None)
    if isinstance(body, dict):
        return body.get(name)
    return getattr(body, name, None)


@pytest.fixture(autouse=True)
def _isolate_psi_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path / ".psi-appdata"))


@pytest.fixture
def _todo_appdata(_isolate_psi_appdata: None) -> Path:
    """The isolated AppData root that todo writes land in.

    Resolved the same way ``resolve_appdata_root()`` does (``PSI_APPDATA`` → absolute),
    so tests can rebuild a write path with ``appdata_todo*_path(str(fixture), sid)``.
    Depends on ``_isolate_psi_appdata`` explicitly: the env var must be set first.
    """
    return Path(os.environ["PSI_APPDATA"]).resolve()
