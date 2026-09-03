from __future__ import annotations

import json
from pathlib import Path

import pytest

from psi_agent.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    language_from_env,
    messages,
    normalize_language,
    t,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("zh-CN", "zh-CN"),
        ("zh", "zh-CN"),
        ("zh_CN", "zh-CN"),
        ("zh-TW", "zh-TW"),
        ("zh_TW", "zh-TW"),
        ("zh-Hant", "zh-TW"),
        ("en-US", "en-US"),
        ("en", "en-US"),
        ("en_US", "en-US"),
        ("fr-FR", "zh-CN"),
        ("", "zh-CN"),
        (None, "zh-CN"),
    ],
)
def test_normalize_language(raw: str | None, expected: str) -> None:
    assert normalize_language(raw) == expected


def test_both_message_files_are_json_objects() -> None:
    package_root = Path(__file__).resolve().parents[3]
    i18n_dir = package_root / "src" / "psi_agent" / "i18n"
    for code in SUPPORTED_LANGUAGES:
        raw = json.loads((i18n_dir / f"{code}.json").read_text(encoding="utf-8"))
        assert isinstance(raw, dict)


def test_messages_have_parity() -> None:
    zh = messages("zh-CN")
    en = messages("en-US")
    assert zh
    assert en
    for code in SUPPORTED_LANGUAGES:
        assert set(messages(code)) == set(zh)


def test_t_returns_translated_string() -> None:
    assert t("language.name", "zh-CN") == "中文"
    assert t("language.name", "zh-TW") == "繁體中文"
    assert t("language.name", "en-US") == "English"


def test_t_falls_back_to_chinese_then_key() -> None:
    assert t("repl.goodbye", "en-US") == "Goodbye!"
    assert t("repl.goodbye", "fr-FR").startswith("再见")
    assert t("missing.key", "en-US") == "missing.key"


def test_t_formats_placeholders() -> None:
    text = t("cli.error", "zh-CN")
    assert text


def test_language_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAITUN_LANG", "en_US")
    assert language_from_env() == "en-US"
    monkeypatch.setenv("HAITUN_LANG", "zh_Hant")
    assert language_from_env() == "zh-TW"
    monkeypatch.delenv("HAITUN_LANG")
    assert language_from_env() == DEFAULT_LANGUAGE
