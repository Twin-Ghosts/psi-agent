"""Feishu sync org tree — build/refresh the LLM wiki 组织树 from the org directory.

Reads the whole company roster (通讯录, recursive ``find_by_department`` from the root
department), resolves each member's direct leader via the batch user-detail endpoint
(``leader_user_id``), builds a tree, then writes it into the LLM wiki: each person's
summary page 《姓名》 gets a 「组织关系」 block (上级 / 下属), and the root page
《公司工作树》 is fully rewritten with the whole tree.

Pure logic (member list -> tree + cycle detection + unresolved leaders) lives in
``_build_org_tree`` so it is unit-testable with no IO; the async tool only does IO
(roster fetch, wiki read/write) around it.
"""

from __future__ import annotations

import json
from typing import Any

# Shared block heading / root page title, referenced by company-todo-sync and
# todo-alignment-check when they read the 组织关系 block or the whole tree.
_ORG_BLOCK_HEADING = "## 组织关系"
_ROOT_PAGE_TITLE = "公司工作树"
_BATCH_SIZE = 50

# Fullwidth punctuation used in the rendered wiki text, built from codepoints so the
# Python source stays free of ambiguous-width characters (RUF001).
_FULL_COLON = chr(0xFF1A)  # fullwidth colon
_FULL_LPAREN = chr(0xFF08)  # fullwidth left paren
_FULL_RPAREN = chr(0xFF09)  # fullwidth right paren
_FULL_COMMA = chr(0xFF0C)  # fullwidth comma


def _build_org_tree(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Build {roots, by_open_id, unresolved_leaders, cycles_detected} from members.

    ``members`` is a list of dicts, each with ``open_id`` / ``name`` /
    ``leader_user_id``. A node whose leader is empty or absent from the set is a
    root; a non-empty leader not in the set is reported in ``unresolved_leaders``.
    Cycles are detected by walking the leader chain and re-visiting a node.
    """
    by_open_id: dict[str, dict[str, Any]] = {}
    for m in members:
        oid = (m.get("open_id") or m.get("user_id") or "").strip()
        if not oid:
            continue
        by_open_id[oid] = {
            "name": m.get("name", "") or oid,
            "leader_open_id": (m.get("leader_user_id") or "").strip(),
            "reports": [],
        }

    unresolved: set[str] = set()
    for oid, node in by_open_id.items():
        leader = node["leader_open_id"]
        if not leader:
            continue
        if leader in by_open_id:
            if leader != oid:
                by_open_id[leader]["reports"].append(oid)
        else:
            unresolved.add(leader)

    roots = sorted(
        oid
        for oid, node in by_open_id.items()
        if not node["leader_open_id"] or node["leader_open_id"] not in by_open_id
    )
    return {
        "roots": roots,
        "by_open_id": by_open_id,
        "unresolved_leaders": sorted(unresolved),
        "cycles_detected": _detect_cycles(by_open_id),
    }


def _detect_cycles(by_open_id: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Return the leader-graph cycles, each as a closed walk of open_ids."""
    color: dict[str, int] = dict.fromkeys(by_open_id, 0)  # 0 white, 1 gray, 2 black
    cycles: list[list[str]] = []

    def walk(oid: str, path: list[str]) -> None:
        color[oid] = 1
        path.append(oid)
        leader = by_open_id[oid]["leader_open_id"]
        if leader in by_open_id:
            if color[leader] == 1:
                start = path.index(leader)
                cycles.append([*path[start:], leader])
            elif color[leader] == 0:
                walk(leader, path)
        color[oid] = 2
        path.pop()

    for start in by_open_id:
        if color[start] == 0:
            walk(start, [])
    return cycles


def _render_org_block(node: dict[str, Any], by_open_id: dict[str, Any]) -> str:
    """Render the 「组织关系」 block (上级 / 下属) for one person."""
    leader = node["leader_open_id"]
    leader_line = (
        f"- 上级{_FULL_COLON}[[{by_open_id[leader]['name']}]]"
        if leader and leader in by_open_id
        else f"- 上级{_FULL_COLON}{_FULL_LPAREN}无{_FULL_RPAREN}"
    )
    reports = sorted((by_open_id[r]["name"] for r in node["reports"]), key=str)
    reports_line = (
        "- 下属"
        + _FULL_COLON
        + ("、".join(f"[[{n}]]" for n in reports) if reports else _FULL_LPAREN + "无" + _FULL_RPAREN)
    )
    return f"{_ORG_BLOCK_HEADING}\n{leader_line}\n{reports_line}"


def _render_tree(roots: list[str], by_open_id: dict[str, Any]) -> str:
    """Render the whole tree as an indented [[wikilink]] list for the root page."""
    note = f"{_FULL_LPAREN}由 feishu_sync_org_tree 自动生成{_FULL_COMMA}请勿手改{_FULL_RPAREN}"
    lines = [note, ""]

    def emit(oid: str, depth: int) -> None:
        lines.append(f"{'  ' * depth}- [[{by_open_id[oid]['name']}]]")
        for report in sorted(by_open_id[oid]["reports"]):
            emit(report, depth + 1)

    for root in roots:
        emit(root, 0)
    return "\n".join(lines)


def _replace_org_block(body: str, block: str) -> str:
    """Swap any existing 「组织关系」 block for ``block``, preserving everything else."""
    result: list[str] = []
    skipping = False
    for line in body.splitlines():
        if line.strip() == _ORG_BLOCK_HEADING:
            skipping = True
            continue
        if skipping:
            if line.startswith("## "):
                skipping = False
                result.append(line)
            continue
        result.append(line)
    while result and result[-1].strip() == "":
        result.pop()
    result.extend(["", block.rstrip("\n")])
    return "\n".join(result).strip() + "\n"


async def feishu_sync_org_tree(
    root_department_id: str = "0",
    boss_open_id: str = "",
    user_key: str = "",
    identity: str = "",
) -> str:
    """Sync the LLM wiki 组织树 from the Feishu org directory.

    Lists every member (recursive from ``root_department_id``), resolves each
    member's direct leader via ``leader_user_id``, builds the tree, then writes:
    each person's page 《姓名》 gets a 「组织关系」 block (上级 / 下属), and the
    root page 《公司工作树》 is fully rewritten. Idempotent: re-running with an
    unchanged org produces the same pages.

    Args:
        root_department_id: Org root department id ("0" by convention).
        boss_open_id: Reserved for future use (e.g. granting the boss the root).
        user_key: Reserved for write-ownership parity with other tools; the roster
            read uses the tenant token and the wiki is local, so it is not needed.
        identity: Reserved, same reason as ``user_key``.
    """
    import _feishu_impl as _core  # noqa: PLC0415
    import _llm_wiki_impl as _wiki  # noqa: PLC0415

    roster = await _core.list_department_members_impl(department_id=root_department_id, recursive=True)
    if not roster.get("ok"):
        return json.dumps(roster, ensure_ascii=False, default=str)
    raw_members = roster.get("members", [])

    # find_by_department returns the id under ``user_id`` (the requested type), so
    # with user_id_type="open_id" the open_id lives in that field.
    open_ids = [m.get("user_id") or m.get("open_id") for m in raw_members if isinstance(m, dict)]
    open_ids = [oid for oid in open_ids if oid]

    leaders: dict[str, str] = {}
    for i in range(0, len(open_ids), _BATCH_SIZE):
        chunk = open_ids[i : i + _BATCH_SIZE]
        detail = await _core.get_users_batch_impl(",".join(chunk), user_id_type="open_id")
        if detail.get("ok"):
            for u in detail.get("users", []) if isinstance(detail.get("users"), list) else []:
                oid = u.get("open_id") or u.get("user_id") or ""
                if oid:
                    leaders[oid] = u.get("leader_user_id") or ""

    members: list[dict[str, Any]] = []
    for m in raw_members:
        if not isinstance(m, dict):
            continue
        oid = m.get("user_id") or m.get("open_id") or ""
        members.append({"open_id": oid, "name": m.get("name", ""), "leader_user_id": leaders.get(oid, "")})

    tree = _build_org_tree(members)
    by_open_id = tree["by_open_id"]

    write_errors: list[str] = []
    for node in by_open_id.values():
        block = _render_org_block(node, by_open_id)
        merged = await _merge_org_block(_wiki, node["name"], block)
        if merged is None:
            write_errors.append(node["name"])

    root_res = await _wiki.wiki_write_impl(
        _ROOT_PAGE_TITLE, _render_tree(tree["roots"], by_open_id), tags=["person"], overwrite=True
    )
    if not root_res.get("ok"):
        write_errors.append(_ROOT_PAGE_TITLE)

    result: dict[str, Any] = {
        "ok": True,
        "member_count": len(members),
        "unresolved_leaders": tree["unresolved_leaders"],
        "cycles_detected": tree["cycles_detected"],
        "root_page": _ROOT_PAGE_TITLE,
    }
    if write_errors:
        result["write_errors"] = write_errors
    return json.dumps(result, ensure_ascii=False, default=str)


async def _merge_org_block(wiki: Any, name: str, block: str) -> str | None:
    """Insert/replace the 组织关系 block in one person's page, keeping the rest intact."""
    read = await wiki.wiki_read_impl(name)
    existing = read.get("content", "") if read.get("ok") else ""
    merged = _replace_org_block(existing, block)
    res = await wiki.wiki_write_impl(name, merged, tags=["person"], overwrite=True)
    return merged if res.get("ok") else None
