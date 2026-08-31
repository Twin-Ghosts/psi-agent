"""Accuracy baseline for the guoshu-weekly agent (396-question self-built set).

Runs each question through the real agent loop -- tool selection, MCP取数,
answer composition -- then grades the natural-language answer against the
reference result set with an LLM, per requirement 9.2 ("以 llm 判断为口径").

Why LLM grading and not string comparison: the reference answers are SQL result
sets, the agent produces prose plus tables. "82" and "共 82 项" are the same
answer; a diff is not.  The grader is told to judge only whether the facts match,
and to ignore wording, ordering and formatting.

Per-category rates matter more than the total: M (权限与安全) and N (不可答) are
pass/fail gates -- one miss there is a defect, not a percentage point.

Usage:
    export GUOSHU_WEEKLY_MCP_URL=http://127.0.0.1:18900/mcp
    export GUOSHU_WEEKLY_MCP_TOKEN=demo-token
    export BASELINE_API_KEY=sk-...
    python tests/baseline.py --limit 20            # smoke run
    python tests/baseline.py                       # full 396
    python tests/baseline.py --category M,N        # only the gate categories
"""

# ruff: noqa: RUF001  中文题目与判定提示里的全角标点是数据, 不能换成半角。
# ruff: noqa: T201  这是命令行脚本, stdout 就是它的输出通道。
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx

WORKSPACE = Path(__file__).resolve().parent.parent
REPO_ROOT = WORKSPACE.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from psi_agent.session.tool_registry import ToolRegistry  # noqa: E402

DEFAULT_ANSWERS = Path.home() / "Downloads" / "nl2sql-answers.jsonl"
DEFAULT_MODEL = os.environ.get("BASELINE_MODEL", "deepseek-chat")
DEFAULT_BASE_URL = os.environ.get("BASELINE_BASE_URL", "https://api.deepseek.com/v1")

MAX_TOOL_ROUNDS = 6
"""Cap on tool-call rounds per question.

Enough for 定位 → 取数 → 复核, but bounded: a runaway loop would burn the whole
run's token budget on one question.  Hitting the cap is recorded as a failure
with reason `max_rounds`, not silently treated as an answer.
"""

GRADER_SYSTEM = """\
你是严格的评测判定器。判断「实际回答」是否在事实上等价于「参考答案」。

判定规则：
- 只看事实是否一致：数字、名称、条目集合。措辞、语序、格式、表格样式一律不计。
- 参考答案是 SQL 结果集；实际回答是自然语言，可能含解释与口径说明，这不算错。
- 数字必须一致。参考 82 而回答「约 80」判错。
- 集合类答案：条目齐全且无多余即算对，顺序不计。
- 实际回答多给了口径说明、数据来源声明、依据字段，不影响判定。
- 若参考答案为空集/0，实际回答说明「没有记录/为 0」即算对。
- 若实际回答说「不可答」但参考答案有内容，判错；反之亦然。

只输出 JSON：{"verdict": "pass" 或 "fail", "reason": "一句话理由"}"""


@dataclass
class Outcome:
    qid: str
    category: str
    difficulty: str
    kind: str
    passed: bool
    reason: str
    elapsed: float
    rounds: int
    tools_used: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)


class Upstream:
    """Minimal OpenAI-compatible client.

    Deliberately not the psi-agent AI layer: that needs a running Gateway plus
    sockets, and this harness only needs chat completions.  Keeping it separate
    means a baseline run cannot be broken by Gateway config drift.
    """

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self._key = api_key
        self._model = model
        self._base = base_url.rstrip("/")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        last_error = ""
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
                    response = await client.post(
                        f"{self._base}/chat/completions",
                        headers={"Authorization": f"Bearer {self._key}"},
                        json=payload,
                    )
                    if response.status_code >= 500 or response.status_code == 429:
                        last_error = f"HTTP {response.status_code}"
                        await anyio.sleep(min(2.0 * (2**attempt), 12.0))
                        continue
                    response.raise_for_status()
                    return response.json()
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await anyio.sleep(min(2.0 * (2**attempt), 12.0))
        raise RuntimeError(f"upstream failed after 3 attempts: {last_error}")


def tool_schemas(registry: ToolRegistry) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, meta in sorted(registry.tools.items()):
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": meta.description,
                    "parameters": meta.parameters,
                },
            }
        )
    return out


async def answer_question(
    upstream: Upstream,
    registry: ToolRegistry,
    schemas: list[dict[str, Any]],
    system_prompt: str,
    question: str,
    trace: list[str] | None = None,
) -> tuple[str, int, list[str]]:
    """Run one question through the agent loop. Returns (answer, rounds, tools).

    ``trace`` collects the actual call text (``tool(args)``) plus the final answer.
    Tool NAMES alone cannot tell a right call from a wrong one: every prompt-side
    failure here was the right tool carrying the wrong argument, so diagnosing one
    off the name list means guessing.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]
    used: list[str] = []
    for round_index in range(MAX_TOOL_ROUNDS):
        reply = await upstream.complete(messages, schemas)
        choice = reply["choices"][0]
        message = choice["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            if trace is not None:
                trace.append((message.get("content") or "").strip()[:600])
            return (message.get("content") or "").strip(), round_index + 1, used
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            }
        )
        for call in calls:
            name = call["function"]["name"]
            used.append(name)
            if trace is not None:
                trace.append(f"{name}({(call['function'].get('arguments') or '{}').strip()[:220]})")
            func = registry.get(name)
            if func is None:
                result = json.dumps({"ok": False, "error": {"code": "no_such_tool", "message": name}})
            else:
                try:
                    raw_args = call["function"].get("arguments") or "{}"
                    kwargs = json.loads(raw_args) if raw_args.strip() else {}
                    result = await func(**kwargs)
                except Exception as exc:
                    result = json.dumps(
                        {"ok": False, "error": {"code": "tool_raised", "message": f"{type(exc).__name__}: {exc}"}},
                        ensure_ascii=False,
                    )
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    return "", MAX_TOOL_ROUNDS, used


async def grade(upstream: Upstream, question: str, gold: Any, actual: str) -> tuple[bool, str]:
    if not actual:
        return False, "空回答（可能撞到 max_rounds）"
    gold_text = json.dumps(gold, ensure_ascii=False)
    if len(gold_text) > 4000:
        gold_text = gold_text[:4000] + " …（参考答案已截断）"
    user = f"问题：{question}\n\n参考答案：{gold_text}\n\n实际回答：{actual[:4000]}"
    reply = await upstream.complete(
        [{"role": "system", "content": GRADER_SYSTEM}, {"role": "user", "content": user}],
        temperature=0.0,
    )
    text = (reply["choices"][0]["message"].get("content") or "").strip()
    verdict, reason = _parse_verdict(text)
    return verdict, reason


def _parse_verdict(text: str) -> tuple[bool, str]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```")[1] if "```" in stripped[3:] else stripped[3:]
        stripped = stripped.removeprefix("json").strip()
    try:
        parsed = json.loads(stripped)
        return parsed.get("verdict") == "pass", str(parsed.get("reason", ""))[:160]
    except ValueError, AttributeError:
        # Fall back to a keyword read rather than discarding the judgement.
        lowered = stripped.lower()
        if '"pass"' in lowered or lowered.startswith("pass"):
            return True, "（判定器未返回合法 JSON，按关键词读取）"
        return False, f"判定器输出无法解析：{stripped[:100]}"


async def load_system_prompt() -> str:
    spec = importlib.util.spec_from_file_location("guoshu_baseline_system", WORKSPACE / "systems" / "system.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load systems/system.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return await module.system_prompt_builder()


def summarise(results: list[Outcome], elapsed: float) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    print("\n" + "=" * 72)
    print(f"总体：{passed}/{total} = {100 * passed / max(total, 1):.1f}%   耗时 {elapsed / 60:.1f} min")
    print("=" * 72)

    by_category: dict[str, list[Outcome]] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)
    print("\n按大类（M 权限与安全、N 不可答为闸门类，错一道即属缺陷）：")
    for category in sorted(by_category):
        group = by_category[category]
        ok = sum(1 for r in group if r.passed)
        gate = "  ← 闸门" if category.startswith(("M", "N")) else ""
        print(f"  {category:<24} {ok:>3}/{len(group):<3} {100 * ok / len(group):>5.1f}%{gate}")

    by_difficulty: dict[str, list[Outcome]] = {}
    for r in results:
        by_difficulty.setdefault(r.difficulty, []).append(r)
    print("\n按难度：")
    for level in ("easy", "medium", "hard", "expert"):
        group = by_difficulty.get(level)
        if group:
            ok = sum(1 for r in group if r.passed)
            print(f"  {level:<8} {ok:>3}/{len(group):<3} {100 * ok / len(group):>5.1f}%")

    by_kind: dict[str, list[Outcome]] = {}
    for r in results:
        by_kind.setdefault(r.kind, []).append(r)
    print("\n按答案形态：")
    for kind in sorted(by_kind):
        group = by_kind[kind]
        ok = sum(1 for r in group if r.passed)
        print(f"  {kind:<8} {ok:>3}/{len(group):<3} {100 * ok / len(group):>5.1f}%")

    latencies = sorted(r.elapsed for r in results)
    if latencies:
        mid = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95) - 1] if len(latencies) >= 20 else latencies[-1]
        over10 = sum(1 for x in latencies if x > 10)
        over30 = sum(1 for x in latencies if x > 30)
        print(f"\n单题耗时：中位 {mid:.1f}s  p95 {p95:.1f}s  最长 {latencies[-1]:.1f}s")
        print(f"  超 10s：{over10} 题；超 30s：{over30} 题（验收：简单 ≤10s、困难 ≤30s）")
        print("  注：本机 mock 库 1.1MB，此数不可外推到真实库")

    failures = [r for r in results if not r.passed]
    if failures:
        print(f"\n失败明细（{len(failures)} 题）：")
        for r in failures[:40]:
            print(f"  {r.qid:<10} {r.category[:18]:<18} {r.reason[:80]}")
        if len(failures) > 40:
            print(f"  …另有 {len(failures) - 40} 题，见 JSON 报告")


async def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("BASELINE_API_KEY", "")
    if not api_key:
        print("BASELINE_API_KEY 未设置", file=sys.stderr)
        return 2
    answers_path = anyio.Path(args.answers)
    if not await answers_path.exists():
        print(f"测试集不存在：{answers_path}", file=sys.stderr)
        return 2

    raw_lines = (await answers_path.read_text(encoding="utf-8")).splitlines()
    questions = [json.loads(line) for line in raw_lines if line.strip()]
    if args.category:
        wanted = {c.strip().upper() for c in args.category.split(",")}
        questions = [q for q in questions if q["category"][:1].upper() in wanted]
    if args.ids:
        # Run only the listed ids. Whether a fix worked is read off whether its
        # target questions recover across runs, not off the total: the same code
        # once flipped 28 questions in both directions between two full runs.
        picked = {q.strip().upper() for q in args.ids.split(",") if q.strip()}
        questions = [q for q in questions if q["id"].upper() in picked]
        missing = picked - {q["id"].upper() for q in questions}
        if missing:
            print(f"题号不存在：{', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        print("筛选后没有题目", file=sys.stderr)
        return 2

    upstream = Upstream(api_key, args.model, args.base_url)
    registry = await ToolRegistry.load(WORKSPACE / "tools", "baseline")
    schemas = tool_schemas(registry)
    system_prompt = await load_system_prompt()
    print(f"题目 {len(questions)} 道，工具 {len(schemas)} 个，模型 {args.model}，并发 {args.concurrency}")

    results: list[Outcome] = []
    limiter = anyio.CapacityLimiter(args.concurrency)
    started = time.monotonic()
    done = 0

    async def work(item: dict[str, Any]) -> None:
        nonlocal done
        async with limiter:
            begin = time.monotonic()
            trace: list[str] | None = [] if args.trace else None
            try:
                actual, rounds, used = await answer_question(
                    upstream, registry, schemas, system_prompt, item["question"], trace
                )
                spent = time.monotonic() - begin
                if rounds >= MAX_TOOL_ROUNDS and not actual:
                    ok, reason = False, "max_rounds：工具轮次用尽仍未给出回答"
                else:
                    ok, reason = await grade(upstream, item["question"], item["gold_answer"], actual)
            except Exception as exc:
                spent = time.monotonic() - begin
                ok, reason, rounds, used = False, f"harness 异常：{type(exc).__name__}: {exc}"[:160], 0, []
            results.append(
                Outcome(
                    qid=item["id"],
                    category=item["category"],
                    difficulty=item["difficulty"],
                    kind=item["kind"],
                    passed=ok,
                    reason=reason,
                    elapsed=spent,
                    rounds=rounds,
                    tools_used=used,
                    trace=trace or [],
                )
            )
            done += 1
            mark = "." if ok else "F"
            print(mark, end="", flush=True)
            if done % 50 == 0:
                print(f" {done}/{len(questions)}", flush=True)

    async with anyio.create_task_group() as group:
        for item in questions:
            group.start_soon(work, item)

    elapsed = time.monotonic() - started
    results.sort(key=lambda r: r.qid)
    summarise(results, elapsed)

    report = anyio.Path(args.report)
    await report.write_text(
        json.dumps(
            {
                "model": args.model,
                "total": len(results),
                "passed": sum(1 for r in results if r.passed),
                "elapsed_seconds": round(elapsed, 1),
                "store": "本机 MySQL 8.4 + weekly_mock（演示数据）",
                "results": [
                    {
                        "id": r.qid,
                        "category": r.category,
                        "difficulty": r.difficulty,
                        "kind": r.kind,
                        "passed": r.passed,
                        "reason": r.reason,
                        "elapsed": round(r.elapsed, 2),
                        "rounds": r.rounds,
                        "tools": r.tools_used,
                        "trace": r.trace,
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n报告已写入 {report}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="396 题准确率基线")
    parser.add_argument("--answers", default=str(DEFAULT_ANSWERS))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题")
    parser.add_argument("--category", default="", help="只跑指定大类，如 M,N")
    parser.add_argument("--ids", default="", help="只跑指定题号，如 K5-01,F2-02")
    parser.add_argument("--trace", action="store_true", help="报告里记下每次工具调用的实际入参")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--report", default="baseline-report.json")
    args = parser.parse_args()
    if not os.environ.get("GUOSHU_WEEKLY_MCP_URL"):
        print("GUOSHU_WEEKLY_MCP_URL 未设置；先起 mock 服务", file=sys.stderr)
        return 2
    return anyio.run(run, args)


if __name__ == "__main__":
    raise SystemExit(main())
