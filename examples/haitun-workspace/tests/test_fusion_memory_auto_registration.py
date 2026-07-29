from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

config_module: Any = importlib.import_module("_fusion_memory_config")


@pytest.mark.anyio
async def test_resolve_memory_config_auto_registers_missing_feishu_user(tmp_path, monkeypatch) -> None:
    token_map = tmp_path / "tokens.json"
    token_map.write_text("{}", encoding="utf-8")
    calls: list[dict[str, object]] = []

    async def fake_register(*, url: str, app_id: str, app_secret: str, organization_id: str, feishu_open_id: str, display_name: str | None) -> dict[str, object]:
        calls.append(
            {
                "url": url,
                "app_id": app_id,
                "app_secret": app_secret,
                "organization_id": organization_id,
                "feishu_open_id": feishu_open_id,
                "display_name": display_name,
            }
        )
        return {"ok": True, "result": {"token": "memory-token", "member": {"organization_id": organization_id}}}

    monkeypatch.setattr(config_module, "_register_feishu_user", fake_register)
    cfg = config_module.build_memory_config(
        {
            "FUSION_MEMORY_MCP_URL": "http://127.0.0.1:8700/mcp",
            "FUSION_MEMORY_TOKEN_MAP_FILE": str(token_map),
            "FUSION_MEMORY_AUTO_REGISTER_FEISHU": "1",
            "FUSION_MEMORY_ORGANIZATION_ID": "org-a",
            "PSI_FEISHU_APP_ID": "cli_app",
            "PSI_FEISHU_APP_SECRET": "secret",
        }
    )

    resolved = await config_module.resolve_memory_config("feishu-ou_a", cfg)

    assert resolved.token == "memory-token"
    assert calls == [
        {
            "url": "http://127.0.0.1:8700/mcp",
            "app_id": "cli_app",
            "app_secret": "secret",
            "organization_id": "org-a",
            "feishu_open_id": "ou_a",
            "display_name": None,
        }
    ]
    assert json.loads(token_map.read_text(encoding="utf-8")) == {
        "ou_a": {"token": "memory-token", "workspace_id": "haitun"}
    }


@pytest.mark.anyio
async def test_resolve_memory_config_requires_registration_credentials_when_enabled(tmp_path) -> None:
    token_map = tmp_path / "tokens.json"
    token_map.write_text("{}", encoding="utf-8")
    cfg = config_module.build_memory_config(
        {
            "FUSION_MEMORY_MCP_URL": "http://127.0.0.1:8700/mcp",
            "FUSION_MEMORY_TOKEN_MAP_FILE": str(token_map),
            "FUSION_MEMORY_AUTO_REGISTER_FEISHU": "1",
            "FUSION_MEMORY_ORGANIZATION_ID": "org-a",
        }
    )

    with pytest.raises(config_module.MemoryConfigError, match="registration"):
        await config_module.resolve_memory_config("feishu-ou_a", cfg)
