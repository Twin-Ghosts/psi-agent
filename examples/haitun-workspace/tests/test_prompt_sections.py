"""Regression tests for high-risk workspace prompt guidance."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

SYSTEMS_DIR = Path(__file__).resolve().parents[1] / "systems"
if str(SYSTEMS_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEMS_DIR))

TOOLS_MD = Path(__file__).resolve().parents[1] / "TOOLS.md"

sections = importlib.import_module("prompt_sections")


def test_document_guidance_uses_existing_tools_without_runtime_install() -> None:
    combined = sections.SEND_FILES_SECTION + sections.DELIVERABLES_AS_FILES_SECTION

    assert "Do not run pip install" in combined
    assert "call `write_word`" in combined
    assert "install a library" not in combined


def test_long_structured_deliverables_are_file_first() -> None:
    assert "do not draft the full artifact in chat first" in sections.DELIVERABLES_AS_FILES_SECTION


def test_delivery_forbids_using_feishu_tools_as_the_transport() -> None:
    """File delivery is ``[SEND:]`` on every channel, never a ``feishu_*`` call.

    Regression (web session ``426a743c``): asked only to convert a document, the
    agent called ``feishu_chat_find(name="Haitun团队")`` to deliver it to a Feishu
    group the user never mentioned. On the web console that reaches nobody.
    """
    section = sections.SEND_FILES_SECTION

    assert "Do NOT use `feishu_*` tools" in section
    # ``<feishu_context>`` is the only block that exists, so the rule keys off
    # its *absence* rather than naming every non-Feishu channel's own block.
    assert "<feishu_context>" in section
    assert "Assume not-Feishu unless that block is present" in section
    # Explicitly carves out the legitimate case, so the rule is not over-read.
    assert "explicitly asks" in section


def test_delivery_section_does_not_claim_a_specific_channel() -> None:
    """``[SEND:]`` is channel-agnostic; the prompt must not imply otherwise."""
    section = sections.SEND_FILES_SECTION

    assert "you do not choose a channel" in section
    assert "Feishu chat window" not in section
    assert "飞书聊天窗口" not in section


def test_tools_md_delivery_item_is_channel_neutral() -> None:
    """TOOLS.md item 14 must not describe ``[SEND:]`` as a Feishu-only mechanism.

    It used to read "上传发送到用户当前的飞书聊天窗口" unconditionally, which is
    where the Feishu framing came from — the same text is loaded on the web
    console, where Feishu is the wrong destination.
    """
    text = TOOLS_MD.read_text(encoding="utf-8")

    assert "上传发送到用户当前所在的聊天窗口" in text
    assert "上传发送到用户当前的飞书聊天窗口" not in text
    assert "绝不要拿 `feishu_*` 工具当交付手段" in text
