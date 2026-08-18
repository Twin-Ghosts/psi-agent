"""Operator command for replaying a Dolphin-Agent memory outbox."""

from __future__ import annotations

import os
from dataclasses import dataclass

from loguru import logger

from psi_agent._appdata import appdata_memory_outbox_path, resolve_appdata_root
from psi_agent._logging import setup_logging
from psi_agent.memory.connector import MemoryTurnConnector
from psi_agent.memory.mcp_client import MemoryMcpClient
from psi_agent.memory.outbox import DurableTurnOutbox


@dataclass
class DolphinMemorySync:
    """Flush pending completed Turns to the configured memory MCP service."""

    service_url: str
    session_id: str
    project_id: str
    appdata: str = ""
    service_name: str = "ontology-memory"
    source_name: str = "dolphin-agent"
    token: str = ""
    token_env: str = "PSI_MEMORY_TOKEN"
    timeout_seconds: float = 30.0
    batch_limit: int = 100
    verbose: bool = False

    async def run(self) -> None:
        setup_logging(verbose=self.verbose)
        if self.batch_limit < 1:
            raise ValueError("--batch-limit must be positive")
        appdata_root = await resolve_appdata_root(self.appdata)
        token = self.token or os.environ.get(self.token_env, "")
        path = appdata_memory_outbox_path(appdata_root, self.service_name, self.session_id)
        outbox = DurableTurnOutbox(path)
        client = MemoryMcpClient(self.service_url, token=token, timeout_seconds=self.timeout_seconds)
        connector = MemoryTurnConnector(
            outbox,
            client,
            project_id=self.project_id,
            source_name=self.source_name,
            session_id=self.session_id,
            batch_limit=self.batch_limit,
        )
        before = len(await outbox.peek())
        await connector.flush()
        logger.info(f"memory outbox synchronized: {before} pending item(s)")
