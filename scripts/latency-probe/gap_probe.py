"""gap_probe.py —— 找「日志里什么都没发生」的空档, 交代每个空档夹在哪两条之间。

    docker logs psi-agent-gateway 2>&1 | python3 gap_probe.py
    docker logs psi-agent-gateway 2>&1 | python3 gap_probe.py --min-ms 5000 --session feishu-ou_a

排队、锁等待、上游 TTFB 这几类慢都长成同一个样子: 两条日志之间一段静默。这个脚本不猜
原因, 只把空档连同**前后两条行的身份**一起摆出来 —— 原因由那两条决定, 而不是由脚本的
分类逻辑决定 (上一轮排查反复推翻结论, 有一半是因为脚本先替人下了结论)。

**不喂 stdin 会报错退出** (见 parse.py)。判据与腐化风险同上。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from itertools import pairwise

from parse import Record, read_stdin_records


def _label(record: Record) -> str:
    """一条行的身份 —— 事件类型优先, 没命中判据就退回 ``模块:函数``。

    退回而不是丢弃: 空档的一端很可能是判据还没覆盖的日志, 那恰恰是要看见的东西。
    """
    return record.kind or f"{record.module.rsplit('.', 1)[-1]}:{record.func}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-ms", type=int, default=2000, help="only gaps at least this long")
    ap.add_argument("--session", default="", help="only gaps between lines of this session")
    ap.add_argument("--top", type=int, default=25, help="rows to print")
    args = ap.parse_args()

    records = read_stdin_records()
    if args.session:
        records = [r for r in records if r.session == args.session]
        if len(records) < 2:
            sys.stderr.write(f"error: fewer than 2 lines for session {args.session!r}\n")
            return 2

    gaps: list[tuple[float, Record, Record]] = []
    for before, after in pairwise(records):
        delta = after.seconds - before.seconds
        # 跨零点: 日志格式只有时分秒, 所以午夜会看到一个约 -86400 的跳变。丢掉这一对,
        # 别把它当成负空档 (也别取绝对值 —— 那会伪造出一个 24 小时的空档)。
        if delta < 0:
            continue
        if delta * 1000 >= args.min_ms:
            gaps.append((delta, before, after))

    total_span = records[-1].seconds - records[0].seconds
    gap_total = sum(d for d, _b, _a in gaps)
    sys.stdout.write(f"lines: {len(records)}, span: {total_span:.1f}s\n")
    sys.stdout.write(
        f"gaps >= {args.min_ms}ms: {len(gaps)}, totalling {gap_total:.1f}s "
        f"({100.0 * gap_total / total_span:.1f}% of the span)\n"
        if total_span > 0
        else f"gaps >= {args.min_ms}ms: {len(gaps)}\n"
    )

    if not gaps:
        # 明说「没有空档」而不是打一张空表: 空表看起来太像脚本没跑对。
        sys.stdout.write(f"\nno gap reached {args.min_ms}ms — the log is dense at this threshold.\n")
        return 0

    pairs = Counter(f"{_label(b)} -> {_label(a)}" for _d, b, a in gaps)
    sys.stdout.write("\nmost common boundaries (what the silence sits between):\n")
    for pair, count in pairs.most_common(10):
        subtotal = sum(d for d, b, a in gaps if f"{_label(b)} -> {_label(a)}" == pair)
        sys.stdout.write(f"  {count:5d}x  {subtotal:8.1f}s total  {pair}\n")

    sys.stdout.write(f"\nlongest {min(args.top, len(gaps))} gap(s):\n")
    for delta, before, after in sorted(gaps, key=lambda g: g[0], reverse=True)[: args.top]:
        session = before.session or after.session or "-"
        sys.stdout.write(f"  {delta * 1000:9.0f}ms  {session:24s}  {_label(before)} -> {_label(after)}\n")
        sys.stdout.write(f"             before: {before.message[:110]}\n")
        sys.stdout.write(f"             after : {after.message[:110]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
