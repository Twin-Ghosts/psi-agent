"""私密文件空间守卫 —— 判断当前 session 能不能碰某个路径。

背景: 所有 session 是同一个 gateway 进程内的 async 任务, 同一个 uid, 所以文件
权限位之类的 OS 级隔离用不了; 容器内 ``unshare`` 被拒、``bwrap`` 未装。边界只能
在工具层收口, 收口点是 ``_runtime_paths.resolve_under()`` 与 ``bash``。

配置: ``PSI_PRIVATE_OPEN_IDS`` 逗号分隔的飞书 open_id 白名单。留空 → 全部放行,
本模块等于不存在 (未配置不改变任何现有行为)。

**单向**: 私密用户照常读写公共区; 其他 session 碰不到私密区任何路径。

诚实说明: ``bash`` 那一层是字符串启发式, 挡得住「让海豚看看某人的文件」这类真实
场景, 挡不住刻意的变量拼接 / base64 绕过。要强隔离得上独立容器。
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from psi_agent.session.runtime_context import get_workspace as _runtime_workspace
except ImportError:  # pragma: no cover — standalone import without editable install

    def _runtime_workspace() -> str:
        return ""


PRIVATE_DIRNAME = ".private"
"""私密区在 workspace root 下的固定目录名 (点号开头, 常规列举天然不显示)。"""

_ENV_KEY = "PSI_PRIVATE_OPEN_IDS"


def private_open_ids() -> list[str]:
    """已登记的私密用户 open_id (顺序保留, 去空去重)。"""
    raw = os.environ.get(_ENV_KEY, "")
    out: list[str] = []
    for piece in raw.replace(";", ",").split(","):
        text = piece.strip()
        if text and text not in out:
            out.append(text)
    return out


def _real(path: str | os.PathLike[str]) -> str:
    """真实路径 —— 展开 symlink 与 ``..``, 防绕过。

    路径可能还不存在 (写入新文件), ``realpath`` 对不存在的尾段也能规范化。
    """
    return os.path.realpath(os.path.abspath(str(path)))


def private_dir(open_id: str, workspace_root: str) -> str:
    """某个私密用户的私密区绝对路径。"""
    return os.path.join(str(workspace_root), PRIVATE_DIRNAME, open_id)


def owner_of(path: str | os.PathLike[str]) -> str | None:
    """*path* 落在哪个私密用户的私密区内; 不在任何私密区则 ``None``。

    按 ``<任意父目录>/.private/<open_id>`` 结构识别, 不依赖 workspace root 的
    具体取值 —— 因为其他 session 的 workspace 指向别处, 手里没有对方的 root。
    """
    ids = private_open_ids()
    if not ids:
        return None
    parts = Path(_real(path)).parts
    for index, name in enumerate(parts):
        if name != PRIVATE_DIRNAME:
            continue
        if index + 1 < len(parts) and parts[index + 1] in ids:
            return parts[index + 1]
    return None


def current_owner(workspace: str = "") -> str | None:
    """当前 session 属于哪个私密用户; 公共 session 则 ``None``。"""
    ws = (workspace or _runtime_workspace() or "").strip()
    if not ws:
        return None
    return owner_of(ws)


def denial_reason(path: str | os.PathLike[str], *, workspace: str = "") -> str | None:
    """越界返回给模型看的拒绝话术, 放行返回 ``None``。

    刻意不抛裸异常 —— 让模型能把「这是别人的私密资料」如实转告用户, 而不是吐一段
    看不懂的 traceback。
    """
    target = owner_of(path)
    if target is None:
        return None  # 不在任何私密区 → 公共区, 放行 (单向: 私密用户也能用公共区)
    if current_owner(workspace) == target:
        return None  # 自己的私密区
    return (
        f"拒绝访问: {path} 属于其他用户的私密文件空间, 本会话无权读写。"
        "请如实告知用户该资料属于他人私密空间, 不要尝试用别的路径或命令绕过。"
    )


def check_access(path: str | os.PathLike[str], *, workspace: str = "") -> None:
    """越界即抛 ``PermissionError`` (消息即 :func:`denial_reason` 的话术)。"""
    reason = denial_reason(path, workspace=workspace)
    if reason:
        raise PermissionError(reason)


def scan_command(command: str, *, workspace: str = "") -> str | None:
    """shell 命令串里是否提到别人的私密路径; 命中返回拒绝话术。

    做法: 命令里凡出现 ``.private`` 字面量, 就把周围的路径样 token 抽出来逐个按
    :func:`denial_reason` 判。同时兜住相对路径 —— 若当前 session 不是私密用户,
    ``.private`` 本身在公共 root 下也不该被翻。
    """
    if not private_open_ids() or PRIVATE_DIRNAME not in command:
        return None

    mine = current_owner(workspace)
    ws = (workspace or _runtime_workspace() or "").strip() or os.getcwd()

    for token in _path_tokens(command):
        if PRIVATE_DIRNAME not in token:
            continue
        candidate = token if os.path.isabs(token) else os.path.join(ws, token)
        reason = denial_reason(candidate, workspace=workspace)
        if reason:
            return reason
        # token 提到 .private 却指不到具体某人 (如 ``ls .private``) —— 公共 session
        # 一律拦, 免得靠列目录反推出谁有私密区。
        if owner_of(candidate) is None and mine is None:
            return f"拒绝访问: {token} 指向私密文件空间目录, 本会话无权列举或读取。请如实告知用户, 不要尝试绕过。"
    return None


_TOKEN_SEPARATORS = " \t\n\r|;&<>()'\"`"


def _path_tokens(command: str) -> list[str]:
    """按 shell 分隔符切出路径样 token (够用即可, 不做完整 shell 解析)。"""
    out: list[str] = []
    current = ""
    for char in command:
        if char in _TOKEN_SEPARATORS:
            if current:
                out.append(current)
                current = ""
        else:
            current += char
    if current:
        out.append(current)
    return out
