"""tail_union.py —— 一张表交代每轮的时间去哪了 (墙钟 / 模型 / 工具 / 其余)。

    docker logs psi-agent-gateway 2>&1 | python3 tail_union.py
    docker logs psi-agent-gateway 2>&1 | python3 tail_union.py --session feishu-ou_abc
    docker logs psi-agent-gateway 2>&1 | python3 tail_union.py --slowest 20

「union」指把三处证据并到同一行上: AI 侧的模型耗时、agent 侧的工具耗时、handle_request
的整轮墙钟。**不喂 stdin 会报错退出**, 不会给你一张假的全零表 (见 parse.py)。

判据与腐化风险见 ``parse.py`` 顶部。**模型耗时以 ai/server.py 两端为准**, 不拿 agent
侧配对 —— 原因写在 ``ai/server.py`` 的 ``_TURN_MARKER_OPEN`` 上方。
"""

from __future__ import annotations

import argparse
import sys

from parse import Turn, build_turns, read_stdin_records


def _percent(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:5.1f}%" if whole else "    - "


def _summarise(turns: list[Turn]) -> None:
    """总表。分母只用**收尾了的**轮次 —— 未收尾的没有墙钟, 混进来会把占比压低。"""
    closed = [t for t in turns if t.wall_ms is not None]
    wall = sum(t.wall_ms or 0 for t in closed)
    model = sum(t.model_ms for t in closed)
    tool = sum(t.tool_ms for t in closed)

    sys.stdout.write(f"turns: {len(turns)} total, {len(closed)} closed, {len(turns) - len(closed)} still open\n")
    sys.stdout.write(f"sessions: {len({t.session for t in turns})}\n")
    if not closed:
        # 有轮次但一个都没收尾 —— 常见于 docker logs --tail 截断。明说, 别给零。
        sys.stdout.write("\nno closed turns: every turn is missing its completion line (log truncated?).\n")
        return

    sys.stdout.write("\n            total_ms      share    per_turn_ms\n")
    for label, value in (("wall", wall), ("model", model), ("tool", tool)):
        sys.stdout.write(f"{label:>10}  {value:10d}  {_percent(value, wall)}  {value // len(closed):10d}\n")
    # 「其余」= 墙钟 - 模型 - 工具。可以是负数, 因为工具并发执行时其耗时之和会超过墙钟
    # 占用的那段。负数本身就是信息 (说明这一批轮次工具并发度高), 故不夹到 0。
    other = wall - model - tool
    sys.stdout.write(f"{'other':>10}  {other:10d}  {_percent(other, wall)}  {other // len(closed):10d}\n")

    unpaired = sum(t.unpaired_ai_calls for t in turns)
    ai_calls = sum(len(t.ai_calls) for t in turns)
    # ** 配平判据 **: 两端计数必须相等, 即 unpaired == 0。非 0 就说明这份日志被截断,
    # 或者 ai/server.py 又多了一条不记 close 的 return 路径。
    sys.stdout.write(f"\nai calls: {ai_calls}, unpaired (open without close): {unpaired}")
    sys.stdout.write("  [balanced]\n" if unpaired == 0 else "  [NOT BALANCED — see note below]\n")
    if unpaired:
        sys.stdout.write(
            "  Either the log is truncated (docker logs --tail cuts mid-turn), or a new\n"
            "  early-return path in ai/server.py skips its close marker. Check that every\n"
            "  `return` there emits _TURN_MARKER_CLOSE exactly once.\n"
        )

    compactions = sum(t.compactions for t in turns)
    incomplete = sum(1 for t in turns if t.wall_ms is not None and not t.complete)
    sys.stdout.write(f"compaction lines: {compactions}\nturns ending incomplete: {incomplete}\n")


def _table(turns: list[Turn], limit: int) -> None:
    sys.stdout.write(f"\nslowest {min(limit, len(turns))} turn(s) by wall time:\n")
    sys.stdout.write(f"{'wall_ms':>9} {'model':>8} {'tool':>8} {'other':>8} {'ai':>3} {'tl':>3} {'cmp':>3}  session\n")
    ranked = sorted((t for t in turns if t.wall_ms is not None), key=lambda t: t.wall_ms or 0, reverse=True)
    for turn in ranked[:limit]:
        wall = turn.wall_ms or 0
        other = wall - turn.model_ms - turn.tool_ms
        flag = "" if turn.complete else "  (incomplete)"
        sys.stdout.write(
            f"{wall:9d} {turn.model_ms:8d} {turn.tool_ms:8d} {other:8d} "
            f"{len(turn.ai_calls):3d} {len(turn.tools):3d} {turn.compactions:3d}  "
            f"{turn.session or '-'}{flag}\n"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default="", help="only this session id")
    ap.add_argument("--slowest", type=int, default=15, help="rows in the per-turn table (0 to skip)")
    args = ap.parse_args()

    turns = build_turns(read_stdin_records())
    if args.session:
        turns = [t for t in turns if t.session == args.session]
        if not turns:
            sys.stderr.write(f"error: no turns for session {args.session!r}\n")
            return 2

    _summarise(turns)
    if args.slowest > 0:
        _table(turns, args.slowest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
