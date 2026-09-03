from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[3] / "agents" / "desktop"


def test_skill_and_prompt_have_only_local_recall_contract() -> None:
    skill = (ROOT / "skills" / "fusion-memory" / "SKILL.md").read_text(encoding="utf-8")
    prompt = (ROOT / "systems" / "prompt_sections.py").read_text(encoding="utf-8")
    prohibited = {
        "setup",
        "start service",
        "doctor",
        "health",
        "token request",
        "memory_health",
        "mcp",
        "sidecar",
        "watcher",
        "systemd",
        "修改 .env",
        "organization_memory_add",
        "feishu",
    }
    match = re.search(r'FUSION_MEMORY_SECTION = """\\\n(.*?)\\\n"""', prompt, re.DOTALL)
    assert match
    combined = skill + match.group(1)
    assert not {term for term in prohibited if term in combined.casefold()}
    for tool in ("memory_add", "memory_search", "memory_answer_context"):
        assert tool in skill


def test_env_contract_and_public_tool_signatures() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "DASHSCOPE_API_KEY",
        "FUSION_MEMORY_ENABLE_JOURNAL",
        "FUSION_MEMORY_JOURNAL_PATH",
        "FUSION_MEMORY_JOURNAL_FSYNC",
        "FUSION_MEMORY_EMBEDDING_MODEL",
        "FUSION_MEMORY_RERANKER_MODEL",
        "FUSION_MEMORY_MODEL_PROVIDER",
        "FUSION_MEMORY_MODEL_NAME",
        "FUSION_MEMORY_MODEL_API_KEY",
        "FUSION_MEMORY_MODEL_BASE_URL",
    ):
        assert name in env
    assert "FUSION_MEMORY_EMBEDDING_API_KEY=" not in env
    assert "FUSION_MEMORY_RERANKER_API_KEY=" not in env
    tools = ROOT / "tools"
    expected = {"memory_add", "memory_search", "memory_answer_context"}
    for name in expected:
        tree = ast.parse((tools / f"{name}.py").read_text(encoding="utf-8"))
        public = {
            node.name for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
        }
        assert public == {name}
