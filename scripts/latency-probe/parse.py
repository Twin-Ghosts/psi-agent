"""延迟排查探针的公共解析层 —— 日志行 → 结构化记录 → 回合。

**这是重写, 不是从云上搬运。** 云端 ``/root/latency-probe/`` 下那份 (parse.py /
tail_union.py / model_repair.py / gap_probe.py 及另外 9 个脚本) 没有拿到, 本目录是按
「它们要回答什么问题」重新实现的。与云上那份可能的差异见 ``README.md``。

用法一律**从 stdin 读**, 脚本自己不去调 docker logs:

    docker logs psi-agent-gateway 2>&1 | python3 tail_union.py

## 腐化风险 (读之前先看这段)

本层靠**日志文本**定位事件, 刻意**不写死 `函数名:行号`** —— 云上那批脚本正是写死了
``run:576`` / ``_execute_one:692`` / ``handle_chat_completions:142`` 这类坐标, 于是源码
一改行号就全部静默失效 (匹配到 0 条, 输出一张全零表, 不报错)。

即便如此, **消息文本本身仍会腐化**。判据集中在下面的 ``_PATTERNS`` 里, 一处一条注释指
向它在源码中的产地。改那些日志文本时请同步这里, 并跑 ``python3 parse.py --self-check``
(它拿内置样例行验证每条模式仍能匹配上)。
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

# loguru 的 ``<green>`` 等标签在非 tty 上不着色, 但 ``docker logs -t`` 或经过 tty 的
# 转存仍可能带 ANSI —— 一律先剥掉, 否则时间戳前面挂着 ``\x1b[32m`` 会让下面的锚点失配。
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# 一行日志的骨架, 对应 ``psi_agent._logging._FORMAT``:
#   HH:MM:SS.SSS | LEVEL    | <session> | name:function:line - message
#
# ``session`` 列是本轮新加的 (未绑定时为 ``-``)。**旧日志没有这一列**, 所以它做成可选:
# 拿 2026-08 的存档来跑不会整体失配, 只是 session 恒为 None。
_LINE = re.compile(
    r"^(?P<ts>\d{2}:\d{2}:\d{2}\.\d{3})\s*\|\s*"
    r"(?P<level>[A-Z]+)\s*\|\s*"
    r"(?:(?P<session>[^|]*?)\s*\|\s*)?"
    r"(?P<module>[\w.]+):(?P<func>[^:]+):(?P<line>\d+)\s*-\s*"
    r"(?P<message>.*)$"
)

# 会话未绑定时的占位符 —— 与 ``_logging.LOG_SESSION_PLACEHOLDER`` 对齐。
_SESSION_PLACEHOLDER = "-"


@dataclass(frozen=True)
class Record:
    """一行日志。``seconds`` 是当天零点起的秒数 (日志格式只有时分秒, 没有日期)。"""

    seconds: float
    level: str
    session: str | None
    module: str
    func: str
    line: int
    message: str
    raw: str

    @property
    def kind(self) -> str | None:
        """本行命中的事件类型 (见 ``_PATTERNS``), 不命中则 ``None``。"""
        return classify(self)


def parse_clock(ts: str) -> float:
    """``HH:MM:SS.SSS`` → 当天零点起的秒数。"""
    hh, mm, rest = ts.split(":")
    return int(hh) * 3600 + int(mm) * 60 + float(rest)


def parse_line(raw: str) -> Record | None:
    """解析一行; 不是日志行 (traceback、裸 print 等) 返回 ``None``。"""
    clean = _ANSI.sub("", raw.rstrip("\n"))
    m = _LINE.match(clean)
    if m is None:
        return None
    session = (m.group("session") or "").strip()
    return Record(
        seconds=parse_clock(m.group("ts")),
        level=m.group("level"),
        session=None if session in ("", _SESSION_PLACEHOLDER) else session,
        module=m.group("module"),
        func=m.group("func"),
        line=int(m.group("line")),
        message=m.group("message"),
        raw=clean,
    )


# 事件判据。**按 logger 名 (module) + 消息文本前缀匹配, 不认行号。**
#
# 每条给出产地, 改那边的日志文本时请同步这里。``module`` 为 None 表示不限模块。
_PATTERNS: tuple[tuple[str, str | None, re.Pattern[str]], ...] = (
    # psi_agent/ai/server.py 的 _TURN_MARKER_OPEN / _TURN_MARKER_CLOSE。
    # ** 模型耗时以这两条为准, 不要再拿 agent 侧的 "Sending request to AI" 配对 **
    # —— 原因写在 ai/server.py 那两个常量上方 (agent 侧 10% 的轮次没有标记)。
    ("ai_open", "psi_agent.ai.server", re.compile(r"^ai-turn open\b")),
    ("ai_close", "psi_agent.ai.server", re.compile(r"^ai-turn close\b")),
    ("ai_rejected", "psi_agent.ai.server", re.compile(r"^ai-turn rejected\b")),
    # psi_agent/session/agent.py —— 发起行为, 只用于观察, 不做耗时配对。
    ("agent_send", "psi_agent.session.agent", re.compile(r"^Sending request to AI\b")),
    # psi_agent/session/agent.py 的 handle_request: 锁获取 → 队列等待的上界。
    ("lock_acquired", "psi_agent.session.agent", re.compile(r"^Acquired session lock\b")),
    ("turn_done", "psi_agent.session.agent", re.compile(r"^Session request completed\b")),
    ("turn_incomplete", "psi_agent.session.agent", re.compile(r"^Session request incomplete\b")),
    # psi_agent/session/agent.py 的 _execute_one —— elapsed_ms 现在直接写在行上,
    # 不必再靠 "Executing tool" 配对时间戳 (工具是并发的, 配对会把别人的等待算进来)。
    ("tool_result", "psi_agent.session.agent", re.compile(r"^Tool result \(")),
    ("tool_error", "psi_agent.session.agent", re.compile(r"^Tool execution error \(")),
    ("tool_start", "psi_agent.session.agent", re.compile(r"^Executing tool:")),
    # psi_agent/channel/feishu/client.py —— 谁落到了哪个 Session / 谁落进了共享兜底。
    ("routed", "psi_agent.channel.feishu.client", re.compile(r"^routed\b")),
    ("shared_fallback", "psi_agent.channel.feishu.client", re.compile(r"^shared-session fallback:")),
    ("route_failed", "psi_agent.channel.feishu.client", re.compile(r"^Gateway route failed\b")),
    # 压缩。产地是 session 侧的压缩流程, 文本前缀比模块名稳, 故不限模块。
    ("compaction", None, re.compile(r"[Cc]ompaction")),
)

# 带 ``elapsed_ms=<int>`` 的行 —— ai_close 与 tool_result/tool_error 都有。
_ELAPSED = re.compile(r"\belapsed_ms=(\d+)\b")

# ai_close 的终态: ok / upstream_error / client_disconnect / prepare_failed。
_OUTCOME = re.compile(r"\boutcome=(\w+)\b")

# tool_result / tool_error / Executing tool 里的工具名, 形如 ``('bash')``。
_TOOL_NAME = re.compile(r"\((['\"])(?P<name>[^'\"]+)\1\)")


def classify(record: Record) -> str | None:
    """返回 *record* 命中的事件类型, 不命中则 ``None``。"""
    for kind, module, pattern in _PATTERNS:
        if module is not None and record.module != module:
            continue
        if pattern.search(record.message):
            return kind
    return None


def elapsed_ms(record: Record) -> int | None:
    """行内自带的耗时 (毫秒), 没有则 ``None``。

    刻意只读行内字段、不做时间戳相减: 相减在并发场景下必错 (工具并发执行、多会话交错),
    而这个字段是产生日志的那个函数在同一作用域里量的。
    """
    m = _ELAPSED.search(record.message)
    return int(m.group(1)) if m else None


def outcome(record: Record) -> str | None:
    """``ai_close`` 行的终态字段。"""
    m = _OUTCOME.search(record.message)
    return m.group(1) if m else None


def tool_name(record: Record) -> str | None:
    """工具相关行里的工具名。"""
    m = _TOOL_NAME.search(record.message)
    return m.group("name") if m else None


def parse_stream(lines: Iterable[str]) -> Iterator[Record]:
    """逐行解析, 跳过非日志行。"""
    for raw in lines:
        record = parse_line(raw)
        if record is not None:
            yield record


def read_stdin_records() -> list[Record]:
    """从 stdin 读全部记录, **空输入直接报错退出**。

    ** 这个硬闸是本次固化时特意加的 **: 云上那批脚本不喂 stdin 时静默输出一张 ``n=0``
    的全零表、退出码 0, 于是「量到了 0」与「根本没量」长得一模一样 —— 开卡人刚拿到过一
    次这样的假零。宁可退出码非 0 吵一声。
    """
    if sys.stdin is None or sys.stdin.isatty():
        _die(
            "no input on stdin. These scripts read the log from stdin; they do not\n"
            "call docker themselves. Usage:\n"
            "    docker logs psi-agent-gateway 2>&1 | python3 <script>.py"
        )
    raw_lines = sys.stdin.read().splitlines()
    if not raw_lines:
        _die("stdin was empty (0 lines). Refusing to print an all-zero table.")
    records = list(parse_stream(raw_lines))
    if not records:
        _die(
            f"read {len(raw_lines)} line(s) from stdin but none parsed as a psi-agent log line.\n"
            "Either this is not a psi-agent log, or the log format changed — see the\n"
            "corrosion note at the top of parse.py."
        )
    return records


def _die(message: str) -> None:
    """报错退出 —— 探针脚本绝不能静默输出假的零。"""
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(2)


@dataclass
class Turn:
    """一个会话里的一轮: 从锁获取 (或首个事件) 到 ``Session request completed``。

    ``ai_calls`` 是这一轮里 AI 侧的 (open, close) 配对。**模型耗时取自 close 行自带的
    ``elapsed_ms``**, 不用时间戳相减。
    """

    session: str | None
    start: float
    end: float | None = None
    records: list[Record] = field(default_factory=list)
    ai_calls: list[tuple[Record, Record | None]] = field(default_factory=list)
    tools: list[Record] = field(default_factory=list)
    compactions: int = 0
    complete: bool = False

    @property
    def wall_ms(self) -> int | None:
        """整轮墙钟耗时。未收尾的轮次返回 ``None`` (刻意不拿末行冒充结束)。"""
        if self.end is None:
            return None
        return int((self.end - self.start) * 1000)

    @property
    def model_ms(self) -> int:
        """这一轮花在上游模型上的毫秒数 —— 各次 AI 调用 ``elapsed_ms`` 之和。"""
        total = 0
        for _open_rec, close_rec in self.ai_calls:
            if close_rec is not None:
                total += elapsed_ms(close_rec) or 0
        return total

    @property
    def tool_ms(self) -> int:
        """工具耗时之和。工具**并发**执行, 所以这个和可以大于墙钟 —— 刻意不归一化,
        归一化会掩盖「一轮里并发起了 20 个工具」这件值得看见的事。"""
        return sum(elapsed_ms(r) or 0 for r in self.tools)

    @property
    def unpaired_ai_calls(self) -> int:
        """开了没关的 AI 调用数。配平判据: 全量统计里这个数应当为 0。"""
        return sum(1 for _o, c in self.ai_calls if c is None)


def build_turns(records: Iterable[Record]) -> list[Turn]:
    """把记录流按会话切成轮次。

    切分锚点是 ``Acquired session lock`` (轮次开始) 与 ``Session request completed /
    incomplete`` (结束)。会话未知的行 (session 列为 ``-``, 或旧格式没有这一列) 归入
    ``None`` 会话 —— 刻意不丢弃, 丢弃就又变成「静默少算一批」。
    """
    open_turns: dict[str | None, Turn] = {}
    finished: list[Turn] = []

    def current(session: str | None, at: float) -> Turn:
        turn = open_turns.get(session)
        if turn is None:
            turn = Turn(session=session, start=at)
            open_turns[session] = turn
        return turn

    for record in records:
        kind = record.kind
        if kind is None:
            continue
        session = record.session
        if kind == "lock_acquired":
            # 上一轮没收尾就又拿到锁: 收掉旧的 (end 留 None, wall_ms 因此为 None),
            # 而不是把两轮的事件混在一起。
            stale = open_turns.pop(session, None)
            if stale is not None:
                finished.append(stale)
            turn = Turn(session=session, start=record.seconds)
            open_turns[session] = turn
        else:
            turn = current(session, record.seconds)

        turn.records.append(record)

        if kind == "ai_open":
            turn.ai_calls.append((record, None))
        elif kind == "ai_close":
            # 就近配对同会话最后一个未闭合的 open。
            for i in range(len(turn.ai_calls) - 1, -1, -1):
                open_rec, close_rec = turn.ai_calls[i]
                if close_rec is None:
                    turn.ai_calls[i] = (open_rec, record)
                    break
            else:
                # 关而未开 —— 通常是日志被从中间截断 (docker logs --tail)。记成一次
                # 只有 close 的调用, 让配平统计看得见, 而不是默默丢掉。
                turn.ai_calls.append((record, record))
        elif kind in ("tool_result", "tool_error"):
            turn.tools.append(record)
        elif kind == "compaction":
            turn.compactions += 1
        elif kind in ("turn_done", "turn_incomplete"):
            turn.end = record.seconds
            turn.complete = kind == "turn_done"
            finished.append(turn)
            open_turns.pop(session, None)

    finished.extend(open_turns.values())
    finished.sort(key=lambda t: t.start)
    return finished


# 自查样例, 按**当前源码的实际输出**写。提到模块级是为了让仓库的用例能反过来检查
# 「每条判据都有样例」—— 新加一条判据却忘了加样例, 自查照样绿, 那条判据就没人验。
#
# ** 但样例终究是手写的 **: 它和代码一起错、或先写对后来代码改了, 自查都发现不了。
# 真正的防线在 ``tests/psi_agent/test_latency_probe_parse.py`` —— 那边驱动真实代码路
# 径、经真实 ``_FORMAT`` 收行再喂进来。
_SAMPLES: dict[str, str] = {
    "ai_open": "14:15:28.575 | INFO     | feishu-ou_a | psi_agent.ai.server:_forward:209 - ai-turn open",
    "ai_close": (
        "14:15:28.575 | INFO     | feishu-ou_a | psi_agent.ai.server:_forward:357 - "
        "ai-turn close elapsed_ms=8421 outcome=ok"
    ),
    "ai_rejected": (
        "14:15:28.575 | ERROR    | - | psi_agent.ai.server:handle_chat_completions:181 - "
        "ai-turn rejected unparseable body: ValueError()"
    ),
    "agent_send": (
        "14:15:28.575 | INFO     | feishu-ou_a | psi_agent.session.agent:run:710 - Sending request to AI via AiClient"
    ),
    "lock_acquired": (
        "14:15:28.575 | INFO     | feishu-ou_a | psi_agent.session.agent:handle_request:502 - "
        "Acquired session lock, processing request"
    ),
    "turn_done": (
        "14:15:28.575 | INFO     | feishu-ou_a | psi_agent.session.agent:handle_request:512 - "
        "Session request completed (model_completed, model_turns=3)"
    ),
    "turn_incomplete": (
        "14:15:28.575 | WARNING  | feishu-ou_a | psi_agent.session.agent:handle_request:514 - "
        "Session request incomplete: stop_cause=turn_limit, model_finish_reason=None, model_turns=40"
    ),
    "tool_result": (
        "14:15:28.575 | INFO     | feishu-ou_a | psi_agent.session.agent:_execute_one:832 - "
        "Tool result ('bash') elapsed_ms=1204: 'ok'"
    ),
    "tool_error": (
        "14:15:28.575 | ERROR    | feishu-ou_a | psi_agent.session.agent:_execute_one:846 - "
        "Tool execution error ('bash') elapsed_ms=30001: TimeoutError()"
    ),
    "tool_start": (
        "14:15:28.575 | INFO     | feishu-ou_a | psi_agent.session.agent:run:803 - Executing tool: 'bash'({})"
    ),
    "routed": (
        "14:15:28.575 | INFO     | - | psi_agent.channel.feishu.client:ensure:160 - "
        "routed 'ou_abc' -> socket='/tmp/psi/channels/feishu-ou_abc.sock' external=False"
    ),
    "shared_fallback": (
        "14:15:28.575 | INFO     | - | psi_agent.channel.feishu.client:_log_shared_fallback:130 - "
        "shared-session fallback: open_id='ou_x' chat_id='' chat_type='p2p' -> "
        "socket='/tmp/s.sock' (no gateway configured)"
    ),
    "route_failed": (
        "14:15:28.575 | WARNING  | - | psi_agent.channel.feishu.client:resolve_core:1007 - "
        "Gateway route failed for open_id='ou_x' chat_id='', falling back to shared socket '/tmp/s.sock' — E()"
    ),
    "compaction": (
        "14:15:28.575 | INFO     | feishu-ou_a | psi_agent.session.compaction:compact:88 - "
        "Compaction needed: prompt_tokens=120000 > threshold=100000"
    ),
}


def _self_check() -> int:
    """拿内置样例行验证每条模式仍能匹配 —— 判据腐化时这里先红。

    样例是**按当前源码的实际输出**写的。改了那边的日志文本, 这里会失败并点名是哪条。
    """
    samples = _SAMPLES
    failures = 0
    for expected, raw in samples.items():
        record = parse_line(raw)
        if record is None:
            sys.stderr.write(f"FAIL {expected}: line did not parse at all\n")
            failures += 1
            continue
        got = record.kind
        if got != expected:
            sys.stderr.write(f"FAIL {expected}: classified as {got!r}\n")
            failures += 1

    # 字段抽取也要验, 不然「匹配上了但 elapsed_ms 读不出来」会静默变成 0。
    close = parse_line(samples["ai_close"])
    assert close is not None
    if elapsed_ms(close) != 8421 or outcome(close) != "ok":
        sys.stderr.write("FAIL ai_close: elapsed_ms/outcome extraction broke\n")
        failures += 1
    tool = parse_line(samples["tool_result"])
    assert tool is not None
    if elapsed_ms(tool) != 1204 or tool_name(tool) != "bash":
        sys.stderr.write("FAIL tool_result: elapsed_ms/tool_name extraction broke\n")
        failures += 1

    # 旧格式 (没有 session 列) 必须仍能解析, 只是 session 为 None。
    legacy = parse_line("14:15:28.575 | INFO     | psi_agent.ai.server:handle:142 - ai-turn open")
    if legacy is None or legacy.session is not None or legacy.kind != "ai_open":
        sys.stderr.write("FAIL legacy: pre-session-column lines no longer parse\n")
        failures += 1

    if failures:
        sys.stderr.write(f"\n{failures} pattern(s) broke — see the corrosion note in parse.py\n")
        return 1
    sys.stderr.write(f"ok: {len(samples)} patterns + field extraction + legacy format\n")
    return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        raise SystemExit(_self_check())
    sys.stderr.write(__doc__ or "")
    raise SystemExit("parse.py is a library; run --self-check, or use tail_union.py / gap_probe.py")
