"""model_repair.py —— 核对模型耗时的两种量法, 并交代它们差多少。

    docker logs psi-agent-gateway 2>&1 | python3 model_repair.py

「repair」指的是修**上一轮排查的那个错数**: 当时把模型耗时算成占 39.2%, 靠的是拿
``session/agent.py`` 的 "Sending request to AI" 与 AI 侧的完成日志配对。agent 侧的标记只
覆盖部分调用路径, 2331 轮里有 241 轮 (10%) 只有 AI 侧没有 agent 侧, 配不上对的整轮被丢
弃 —— 而丢掉的恰好是走特殊分支的慢轮次, 于是偏差是**系统性偏低**, 真实值是 63.4%。

现在权威量法是 AI 侧两端自带的 ``elapsed_ms`` (见 ``ai/server.py`` 的
``_TURN_MARKER_OPEN`` 上方那段交代)。本脚本并排给出两种量法, 用途有二:

1. 拿旧存档复算时, 说明新旧数字为什么不同 (别再把两个口径的数直接比)。
2. 当作**配平的回归探针**: ``unpaired`` 非 0 就是「有 return 路径漏了 close 标记」或
   「日志被截断」, 两者都得先解决再看占比。

**不喂 stdin 会报错退出** (见 parse.py)。
"""

from __future__ import annotations

import sys

from parse import build_turns, elapsed_ms, outcome, read_stdin_records


def main() -> int:
    turns = build_turns(read_stdin_records())
    closed = [t for t in turns if t.wall_ms is not None]

    ai_open = sum(len(t.ai_calls) for t in turns)
    unpaired = sum(t.unpaired_ai_calls for t in turns)
    agent_send = sum(1 for t in turns for r in t.records if r.kind == "agent_send")

    sys.stdout.write("counts (the balance check)\n")
    sys.stdout.write(f"  AI-side  open markers : {ai_open}\n")
    sys.stdout.write(f"  AI-side  unpaired     : {unpaired}\n")
    sys.stdout.write(f"  agent-side send lines : {agent_send}\n")
    missing = ai_open - agent_send
    if ai_open:
        sys.stdout.write(
            f"  agent-side coverage   : {100.0 * agent_send / ai_open:.1f}% "
            f"({missing} AI call(s) with no agent-side line)\n"
        )
    sys.stdout.write(
        "\n  ** Do not pair agent-side lines to measure model time. ** That is the\n"
        "  mistake this script is named after: the uncovered calls above are exactly\n"
        "  the ones that get silently dropped, and they skew low.\n"
    )
    if unpaired:
        sys.stdout.write(
            f"\n  NOT BALANCED: {unpaired} open marker(s) without a close. Either the log is\n"
            "  truncated, or a `return` in ai/server.py skips _TURN_MARKER_CLOSE. Fix that\n"
            "  before trusting any share below.\n"
        )
    else:
        sys.stdout.write("\n  balanced: every open marker has its close.\n")

    if not closed:
        sys.stdout.write("\nno closed turns — cannot compute a share (log truncated?).\n")
        return 0

    wall = sum(t.wall_ms or 0 for t in closed)
    authoritative = sum(t.model_ms for t in closed)

    # 旧口径复现: 只算那些 agent 侧也留了标记的轮次, 其余整轮丢弃 —— 这正是当年的做法。
    legacy_turns = [t for t in closed if any(r.kind == "agent_send" for r in t.records)]
    legacy_wall = sum(t.wall_ms or 0 for t in legacy_turns)
    legacy_model = sum(t.model_ms for t in legacy_turns)

    sys.stdout.write("\nmodel-time share, two ways\n")
    sys.stdout.write(f"  authoritative (all {len(closed)} closed turns) : {100.0 * authoritative / wall:5.1f}%\n")
    if legacy_wall:
        sys.stdout.write(
            f"  legacy (only the {len(legacy_turns)} turns with an agent-side line): "
            f"{100.0 * legacy_model / legacy_wall:5.1f}%\n"
        )
        dropped = len(closed) - len(legacy_turns)
        sys.stdout.write(f"  turns the legacy method would have discarded: {dropped}\n")
    else:
        sys.stdout.write("  legacy: no turn carries an agent-side line; nothing to compare against.\n")

    # 每次 AI 调用的耗时分布 —— 均值会被少数极慢的调用带偏, 故给分位。
    per_call = sorted(
        elapsed_ms(c) or 0 for t in turns for _o, c in t.ai_calls if c is not None and elapsed_ms(c) is not None
    )
    if per_call:
        sys.stdout.write(f"\nper-AI-call elapsed_ms over {len(per_call)} call(s)\n")
        for name, q in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99)):
            sys.stdout.write(f"  {name}: {per_call[min(int(len(per_call) * q), len(per_call) - 1)]:8d}\n")
        sys.stdout.write(f"  max: {per_call[-1]:8d}\n")

    outcomes: dict[str, int] = {}
    for turn in turns:
        for _o, close in turn.ai_calls:
            if close is None:
                continue
            key = outcome(close) or "unknown"
            outcomes[key] = outcomes.get(key, 0) + 1
    if outcomes:
        sys.stdout.write("\nAI call outcomes\n")
        for key, count in sorted(outcomes.items(), key=lambda kv: -kv[1]):
            sys.stdout.write(f"  {key:20s} {count:6d}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
