"""``psi-agent logs`` —— 清掉存量的 0 字节定向 DEBUG 日志文件。

新进程不再产生空文件 (``_logging`` 的 file sink 带 ``delay=True``, 见那里的注释),
这个命令只处理**已经攒下来的**那批 —— 生产 ``.psi/appdata/logs/`` 下实测 824 个,
多到 ``ls`` 都不可用。

刻意做成显式命令而不是启动时自动清: 多进程容器里另一个进程可能刚 open() 完文件、
还没写第一行, 那一刻它合法地是 0 字节。让「起进程」带上删别人文件的副作用, 换来的
是一个偶发的丢日志。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from rich.console import Console

from psi_agent._logging import debug_log_path, prune_empty_debug_logs


@dataclass
class Logs:
    """Delete zero-byte psi-debug-*.log files left by processes that never logged."""

    # 目录由 PSI_DEBUG_LOG_PATH / PSI_APPDATA 决定 (与写日志时同一套推导), 刻意不给
    # 独立的 --dir: 两处推导一旦分家, 清理的就可能不是写入的那个目录。
    dry_run: bool = False
    """Report the count without deleting anything."""

    async def run(self) -> None:
        # 刻意不调 setup_logging: 这是个一次性人机命令, 不该往 stderr 装 handler。
        # 输出走 rich (仓库规范禁用 print, 见根 AGENTS.md「日志约定」)。
        console = Console()
        directory = os.path.dirname(debug_log_path()) or "."
        count = prune_empty_debug_logs(dry_run=self.dry_run)
        verb = "would remove" if self.dry_run else "removed"
        console.print(f"{verb} {count} empty log file(s) under {directory}")
