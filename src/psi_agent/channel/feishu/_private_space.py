"""私密文件空间守卫 (channel 侧) —— 拦住 ``[SEND:]`` 把私密文件传出去。

Channel 是独立进程, 没有 session 的 ``runtime_context``, 但手里有发送者 open_id,
所以这里按「发送者是不是该私密区的主人」判权, 比在 session 侧绕一圈更直接。

与 workspace 侧 ``tools/_private_space.py`` 同一套约定 (``<root>/.private/<open_id>/``
+ ``PSI_PRIVATE_OPEN_IDS``), 两处刻意各自独立实现 —— src 不依赖 workspace 内容。
未配置 ``PSI_PRIVATE_OPEN_IDS`` 时本模块是空操作。
"""

from __future__ import annotations

import os
from pathlib import Path

PRIVATE_DIRNAME = ".private"
_ENV_KEY = "PSI_PRIVATE_OPEN_IDS"


def private_open_ids() -> list[str]:
    """已登记的私密用户 open_id。"""
    raw = os.environ.get(_ENV_KEY, "")
    out: list[str] = []
    for piece in raw.replace(";", ",").split(","):
        text = piece.strip()
        if text and text not in out:
            out.append(text)
    return out


def owner_of(path: str | os.PathLike[str]) -> str | None:
    """*path* 落在哪个私密用户的私密区内; 不在任何私密区则 ``None``。

    先 ``realpath`` 展开 symlink 与 ``..``, 免得 ``/workspace/pub/../.private/x``
    这类写法绕过。
    """
    ids = private_open_ids()
    if not ids:
        return None
    parts = Path(os.path.realpath(os.path.abspath(str(path)))).parts
    for index, name in enumerate(parts):
        if name != PRIVATE_DIRNAME:
            continue
        if index + 1 < len(parts) and parts[index + 1] in ids:
            return parts[index + 1]
    return None


def blocks_send(path: str | os.PathLike[str], sender_open_id: str | None) -> bool:
    """该文件是否**不许**发给这位发送者。

    单向: 主人自己收得到自己的私密文件; 其他人一律拦。
    """
    owner = owner_of(path)
    if owner is None:
        return False  # 公共区文件, 照常发
    return owner != (sender_open_id or "")
