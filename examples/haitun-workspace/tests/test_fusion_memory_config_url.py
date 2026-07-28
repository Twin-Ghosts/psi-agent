"""URL policy for FUSION_MEMORY_MCP_URL — private-LAN plaintext relaxation.

``validate_mcp_url`` accepts https anywhere, http on loopback, and (this change)
http on RFC1918 private-network IPs so a same-LAN Fusion Memory deployment
without public TLS is reachable. Public-host http stays rejected.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = WORKSPACE_ROOT / "tools" / "_fusion_memory_config.py"


def _load(path: Path, prefix: str) -> Any:
    name = f"{prefix}_{hashlib.sha256(os.urandom(16)).hexdigest()}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture
def config_module() -> Any:
    return _load(CONFIG_PATH, "fusion_memory_config")


@pytest.mark.parametrize(
    "url",
    [
        "https://memory.example.com/mcp",
        "http://localhost:9000/mcp",
        "http://127.0.0.1/mcp",
        "http://[::1]/mcp",
        # RFC1918 private-network hosts over plain http (same-LAN deployment).
        "http://192.168.63.71:8700/mcp",
        "http://10.0.0.5/mcp",
        "http://172.16.0.9:8080/mcp",
    ],
)
def test_validate_mcp_url_accepts_secure_and_trusted_plaintext(config_module: Any, url: str) -> None:
    assert config_module.validate_mcp_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        # Public host over plain http — still rejected.
        "http://memory.example.com/mcp",
        "http://8.8.8.8/mcp",
        # Exact-path / credential / query / fragment rules unaffected.
        "https://memory.example.com/other",
        "https://memory.example.com/mcp/",
        "https://user:pass@memory.example.com/mcp",
        "https://memory.example.com/mcp?x=1",
        "https://memory.example.com/mcp#frag",
    ],
)
def test_validate_mcp_url_rejects_untrusted_or_malformed(config_module: Any, url: str) -> None:
    with pytest.raises(ValueError):
        config_module.validate_mcp_url(url)


def test_validate_mcp_url_empty_returns_empty(config_module: Any) -> None:
    assert config_module.validate_mcp_url("") == ""
    assert config_module.validate_mcp_url("   ") == ""


def test_trusted_plaintext_host_predicate(config_module: Any) -> None:
    trusted = config_module._is_trusted_plaintext_host
    assert trusted("localhost") is True
    assert trusted("127.0.0.1") is True
    assert trusted("192.168.1.1") is True
    assert trusted("10.1.2.3") is True
    assert trusted("172.31.255.255") is True
    assert trusted("8.8.8.8") is False
    assert trusted("memory.example.com") is False
