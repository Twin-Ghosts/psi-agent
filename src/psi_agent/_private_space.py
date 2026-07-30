"""对称隔离守卫 —— 每个飞书会话只能读写自己那块 workspace。

**为什么在应用层做**, Gateway 把所有 Session 跑成同一进程内的 async 任务、同一
uid, 文件权限位 / setuid / 容器边界一个都用不上。唯一能判权的地方是工具调用的入
口, 故这里提供纯函数守卫, 由各收口点主动调用。

**对称模型**(与"白名单少数人私密"相对): 每个路由键得到 ``<root>/<owner>/``, 谁都
只能碰自己那个前缀; ``<root>`` 下的 ``PUBLIC_DIRNAME`` 子目录所有人可读, 供公共
材料共用。群聊的 owner 是 ``chat-<chat_id>`` —— 整群共用一块空间, 与群 Session 共
用上下文一致。

**能力边界(如实声明)**: 路径类工具经 ``resolve_under`` 收口, 是确定性判定; ``bash``
/ ``powershell`` 只能对命令串做**启发式**扫描, 挡得住误用与顺手一 ``cat``, 挡不住
刻意的变量拼接 / base64 / 中转文件。要强隔离须每人独立容器。

守卫默认**关闭**: ``PSI_WORKSPACE_ROOT`` 未配时 ``enabled()`` 为 False, 所有函数退
化成放行, 行为与改动前逐字节一致 —— 故可以先部署再开关。
"""

from __future__ import annotations

import os
import re

# 环境变量: 隔离生效的父目录(通常等于 Gateway --feishu-workspace-root)。
_ROOT_ENV = "PSI_WORKSPACE_ROOT"

# ``<root>`` 下这个子目录是公共区, 所有会话可读(写仍只限自己区, 免得互相覆盖)。
PUBLIC_DIRNAME = "public"

# 会话历史 / todos 等 AppData 文件按 session_id 命名, owner 从中反解。
_SESSION_PREFIX = "feishu-"
_GROUP_SESSION_PREFIX = "feishu-chat-"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def workspace_root() -> str:
    """隔离父目录; 空字符串 = 守卫未启用。"""
    return os.environ.get(_ROOT_ENV, "").strip()


def enabled() -> bool:
    """守卫是否生效。未配 ``PSI_WORKSPACE_ROOT`` 时全部放行。"""
    return bool(workspace_root())


def sanitize(token: str) -> str:
    """把 open_id / chat_id 净化成安全目录段(与 FeishuManager 同款)。"""
    return _UNSAFE.sub("_", token or "")


def owner_from_session_id(session_id: str) -> str:
    """从 session_id 反解 owner 目录名; 非飞书 session 返回 ``""``。

    ``feishu-chat-<chat_id>`` → ``chat-<chat_id>``(群),
    ``feishu-<open_id>`` → ``<open_id>``(私聊)。SPA 手建的 session 不带
    ``feishu-`` 前缀, 无主 → 不受限(它们是本机用户自己的会话)。
    """
    sid = (session_id or "").strip()
    if sid.startswith(_GROUP_SESSION_PREFIX):
        return f"chat-{sanitize(sid.removeprefix(_GROUP_SESSION_PREFIX))}"
    if sid.startswith(_SESSION_PREFIX):
        return sanitize(sid.removeprefix(_SESSION_PREFIX))
    return ""


def owner_dir(owner: str) -> str:
    """owner 的空间绝对路径; owner 或 root 为空时返回 ``""``。"""
    root = workspace_root()
    if not root or not owner:
        return ""
    return os.path.realpath(os.path.join(root, owner))


def public_dir() -> str:
    """公共区绝对路径; 未启用时 ``""``。"""
    root = workspace_root()
    return os.path.realpath(os.path.join(root, PUBLIC_DIRNAME)) if root else ""


def _is_within(path: str, base: str) -> bool:
    """*path* 是否在 *base* 之内(含相等)。两侧都必须已 realpath。"""
    if not base:
        return False
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:
        # 不同盘符 (Windows) → 必然不在其内。
        return False


def owner_of(path: str) -> str:
    """*path* 落在谁的空间里; 公共区与 root 外一律 ``""``(无主)。

    先 ``realpath`` 再判, 故 symlink 与 ``..`` 都被展开 —— 这是不可绕的前提。
    """
    root = workspace_root()
    if not root:
        return ""
    real_root = os.path.realpath(root)
    try:
        real = os.path.realpath(path)
    except OSError:
        return ""
    if not _is_within(real, real_root):
        return ""  # root 之外(系统目录 / agent 包)不属任何人, 由别的机制管。
    rel = os.path.relpath(real, real_root)
    if rel in (".", os.curdir):
        return ""
    first = rel.replace("\\", "/").split("/")[0]
    if first == PUBLIC_DIRNAME:
        return ""  # 公共区无主。
    return first


def check_read(path: str, *, session_id: str) -> str | None:
    """可读则 ``None``, 否则返回拒绝原因(给工具直接当错误串返回)。

    读: 自己的空间 + 公共区 + root 之外(agent 包 / 系统路径)都放行; 只拦**别人**
    名下的路径。
    """
    if not enabled():
        return None
    target_owner = owner_of(path)
    if not target_owner:
        return None
    me = owner_from_session_id(session_id)
    if not me:
        # 无主 session(SPA 手建 / 本机直跑)不受隔离约束。
        return None
    if target_owner == me:
        return None
    return f"[Error] 拒绝访问: {path} 属于另一个用户的私有空间。每位用户的文件互相隔离, 只能访问自己的空间与公共区。"


def check_write(path: str, *, session_id: str) -> str | None:
    """可写则 ``None``, 否则返回拒绝原因。

    写比读严: 公共区也**不允许**写 —— 否则一个人能覆盖公共材料影响所有人。
    """
    if not enabled():
        return None
    me = owner_from_session_id(session_id)
    if not me:
        return None
    root = workspace_root()
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    if not _is_within(real, os.path.realpath(root)):
        return None  # root 之外交给既有逻辑(agent 包等)。
    mine = owner_dir(me)
    if mine and _is_within(real, mine):
        return None
    return f"[Error] 拒绝写入: {path} 不在你的私有空间内。请写到自己的空间(相对路径即可), 公共区与他人空间均为只读。"


def forbidden_dirs(session_id: str) -> list[str]:
    """遍历类工具应整棵跳过的目录(root 下除自己与公共区外的所有 owner 目录)。

    用于 ``search_content`` / ``find_files``: 与逐个 ``check_read`` 等价, 但省掉
    对每个候选文件调一次 realpath 的开销。
    """
    if not enabled():
        return []
    me = owner_from_session_id(session_id)
    if not me:
        return []
    root = workspace_root()
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    out: list[str] = []
    for name in entries:
        if name in (me, PUBLIC_DIRNAME):
            continue
        full = os.path.join(root, name)
        if os.path.isdir(full):
            out.append(os.path.realpath(full))
    return out


def owns_session(candidate_session_id: str, *, session_id: str) -> bool:
    """*candidate_session_id* 的历史是否属于当前会话本人。

    跨 session 历史工具(``sessions_list`` / ``session_keyword_search`` 等)扫的是
    全局 AppData ``histories/*.jsonl``, 不分 workspace —— 对话原文往往比文件更敏
    感, 必须按此过滤成只见自己。
    """
    if not enabled():
        return True
    me = owner_from_session_id(session_id)
    if not me:
        return True
    return owner_from_session_id(candidate_session_id) == me


def scan_command(command: str, *, session_id: str) -> str | None:
    """shell 命令串启发式扫描: 命中他人空间则返回拒绝原因, 否则 ``None``。

    **这一层是启发式, 不是沙箱** —— 显式写出别人的 open_id / 目录名会被挡, 但变量
    拼接(``d=ou_x; cat /ws/$d/f``)、base64、`eval` 之类绕得过去。之所以仍然做: 真实
    泄露几乎都是「顺手 ls 一下别人目录」这种直白形态, 挡住它价值很高; 真正的强隔离
    需要每人独立容器。

    判定方式是「别人的目录名作为独立路径段出现」, 而不是裸子串包含 —— 后者会把
    ``/ws/me/ou_other_notes.md`` 这种自己空间里的正常文件名误伤。
    """
    if not enabled() or not command:
        return None
    me = owner_from_session_id(session_id)
    if not me:
        return None
    root = workspace_root()
    try:
        siblings = [
            name
            for name in os.listdir(root)
            if name not in (me, PUBLIC_DIRNAME) and os.path.isdir(os.path.join(root, name))
        ]
    except OSError:
        return None
    for name in siblings:
        # 前后是路径分隔符/引号/空白/串首尾, 才算命中一个完整目录段。
        if re.search(rf"(^|[\s'\"/\\=:]){re.escape(name)}([\s'\"/\\]|$)", command):
            return (
                f"[Error] 拒绝执行: 命令中引用了另一个用户的私有空间 ({name})。"
                "每位用户的文件互相隔离, 只能访问自己的空间与公共区。"
            )
    return None


def blocks_send(path: str, *, open_id: str, chat_id: str = "", chat_type: str = "") -> bool:
    """``[SEND:]`` 侧判权: True = 该文件不许发给这个会话。

    channel 是独立进程, 没有 ``runtime_context``, 但有会话事实 —— 故这里按
    open_id / chat 事实自行推导 owner, **不依赖 workspace 内容**。
    """
    if not enabled():
        return False
    me = f"chat-{sanitize(chat_id)}" if chat_type in ("group", "topic") and chat_id else sanitize(open_id)
    if not me:
        return True  # 认不出收件人 → 保守拒发。
    target_owner = owner_of(path)
    if not target_owner:
        return False  # 公共区 / root 外的产物可发。
    return target_owner != me
