"""Bypass equivalence check for the session-search read path.

Runs the **pre-change** implementation (extracted from a git ref) and the
current one over the same generated corpus, then compares the 6-tuple
``(session_id, hit_count, score, message_count, snippet_count, history_mtime)``
for every query. ``message_count`` moved from eager to lazy computation, so it
is compared explicitly: the timing changed, the value must not.

Usage::

    PYTHONPATH=src python agents/feishu/bin/compare_session_search.py [--ref HEAD]

Exits non-zero unless every tuple matches. Local corpora are far smaller than
production, so the timings printed show the order of magnitude, not the
production p50.
"""

from __future__ import annotations

# ruff: noqa: T201
import argparse
import importlib
import importlib.util
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import anyio

# Derived from this file's own location so the ``feishu`` and ``desktop`` copies
# stay byte-identical, like every other file in these bin/ dirs.
_PACK_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = _PACK_DIR / "tools"
REPO_ROOT = Path(__file__).resolve().parents[3]
TARGET = f"agents/{_PACK_DIR.name}/tools/_session_helpers.py"

QUERIES = (
    "Docker",
    "docker",  # case-insensitivity must not shift hit_count
    "deploy",
    "gateway restart",
    "会话搜索",  # non-ASCII needle
    "nonexistent-needle-zzz",  # zero-hit path
    "a",  # very common single char: hits nearly every message
)

_ROLES = ("user", "assistant", "tool", "system")
_WORDS = (
    "Docker",
    "deploy",
    "gateway",
    "restart",
    "会话搜索",
    "history",
    "session",
    "alpha",
    "beta",
)


def build_corpus(root: Path, *, sessions: int, messages: int, seed: int = 1234) -> None:
    """Generate a histories/ tree: mixed roles, blank lines, malformed JSON.

    The junk lines matter — both implementations skip them, and the skip rules
    are what ``message_count`` and the score denominator depend on.
    """
    rng = random.Random(seed)
    histories = root / "histories"
    histories.mkdir(parents=True, exist_ok=True)
    for i in range(sessions):
        lines: list[str] = []
        for j in range(rng.randint(1, messages)):
            if rng.random() < 0.04:
                lines.append("")  # blank line
                continue
            if rng.random() < 0.04:
                lines.append("{not json at all")  # malformed
                continue
            role = rng.choice(_ROLES)
            text = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(2, 12)))
            payload: dict[str, Any] = {"role": role, "content": f"msg {j} {text}"}
            if role == "tool":
                payload["name"] = "bash"
            if rng.random() < 0.05:
                payload["content"] = None  # non-str content
            lines.append(json.dumps(payload, ensure_ascii=False))
        # A few sessions get no role-bearing line at all: exercises the
        # message_count == 0 fallback branch.
        if i % 37 == 0:
            lines = ["", "{broken", json.dumps({"norole": 1})]
        (histories / f"s{i:04d}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_old_module(ref: str, workdir: Path) -> Any:
    """Import the pre-change helpers from *ref* under a distinct module name."""
    git = shutil.which("git")
    if git is None:
        msg = "git not found on PATH"
        raise RuntimeError(msg)
    raw = subprocess.run(
        [git, "show", f"{ref}:{TARGET}"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    old_dir = workdir / "old_tools"
    # The helpers import sibling tools by bare name (_background_process_registry
    # …), so the whole tools dir has to come along.
    shutil.copytree(TOOLS_DIR, old_dir)
    (old_dir / "_session_helpers.py").write_bytes(raw)

    sys.path.insert(0, str(old_dir))
    try:
        spec = importlib.util.spec_from_file_location("_session_helpers_old", old_dir / "_session_helpers.py")
        if spec is None or spec.loader is None:
            msg = "could not load the pre-change _session_helpers"
            raise RuntimeError(msg)
        module = importlib.util.module_from_spec(spec)
        sys.modules["_session_helpers_old"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(old_dir))


def tuples_from(result: dict[str, Any]) -> list[tuple[Any, ...]]:
    """The 6-tuple per hit, in returned order (ordering is part of the contract)."""
    out: list[tuple[Any, ...]] = []
    for hit in result.get("hits", []):
        out.append(
            (
                hit.get("session_id"),
                hit.get("hit_count"),
                hit.get("score"),
                hit.get("message_count"),
                len(hit.get("snippets", []) or []),
                hit.get("history_mtime"),
            )
        )
    return out


async def run_case(module: Any, *, query: str, workspace: str, session_id: str = "") -> tuple[list[Any], float]:
    started = time.perf_counter()
    result = await module.keyword_search_sessions(
        query=query,
        session_id=session_id,
        workspace_raw=workspace,
        limit=50,
    )
    elapsed = time.perf_counter() - started
    return tuples_from(result), elapsed


async def main_async(args: argparse.Namespace) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="session-search-cmp-"))
    corpus = workdir / "ws"
    build_corpus(corpus, sessions=args.sessions, messages=args.messages)
    total_bytes = sum(p.stat().st_size for p in (corpus / "histories").glob("*.jsonl"))

    # Point AppData at an empty dir so the dual-read resolves to the corpus and
    # the developer's real history dir cannot leak into the comparison.
    os.environ["PSI_APPDATA"] = str(workdir / "appdata")
    (workdir / "appdata" / "histories").mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(TOOLS_DIR))
    new_mod = importlib.import_module("_session_helpers")
    old_mod = load_old_module(args.ref, workdir)

    print(f"corpus: {args.sessions} files, {total_bytes / 1e6:.1f} MB, ref={args.ref}")
    print(f"{'query':<24} {'old':>8} {'new':>8} {'hits':>6}  identical")
    print("-" * 62)

    all_identical = True
    old_times: list[float] = []
    new_times: list[float] = []
    mismatches: list[str] = []

    cases: list[tuple[str, str]] = [(q, "") for q in QUERIES]
    # Also cover the scoped path, which now skips the full glob.
    cases.append(("Docker", "s0001"))
    cases.append(("Docker", "no-such-session"))

    for query, scope in cases:
        old_tuples, old_dt = await run_case(old_mod, query=query, workspace=str(corpus), session_id=scope)
        new_tuples, new_dt = await run_case(new_mod, query=query, workspace=str(corpus), session_id=scope)
        old_times.append(old_dt)
        new_times.append(new_dt)
        same = old_tuples == new_tuples
        if not same:
            all_identical = False
            mismatches.append(f"query={query!r} scope={scope!r}")
        label = f"{query[:16]!r}" + (f"@{scope}" if scope else "")
        print(f"{label:<24} {old_dt:>7.2f}s {new_dt:>7.2f}s {len(new_tuples):>6}  {same}")

    print("-" * 62)
    print(f"old p50={statistics.median(old_times):.2f}s  new p50={statistics.median(new_times):.2f}s")
    print(f"old total={sum(old_times):.2f}s  new total={sum(new_times):.2f}s")
    if mismatches:
        for line in mismatches:
            print(f"MISMATCH: {line}")
    print(f"ALL_IDENTICAL={all_identical}")
    return 0 if all_identical else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="git ref holding the pre-change implementation")
    parser.add_argument("--sessions", type=int, default=400, help="history files to generate")
    parser.add_argument("--messages", type=int, default=400, help="max messages per file")
    args = parser.parse_args()

    return anyio.run(main_async, args)


if __name__ == "__main__":
    raise SystemExit(main())
