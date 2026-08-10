"""文档详情页的运行时: 建文档、写块、读块、授权、订阅变更 —— 唯一碰飞书文档接口的模块。

刻意为之: 所有飞书调用通过注入的 `api` 可调用对象(签名与 feishu_api 工具一致),
这样单测传 fake 就能跑, 不需要凭据 —— 与 _rookie_sop_store 对 bitable 的做法一致。

一人一份文档: 建好后只把这个新人授权成 edit, 别人无权访问。文档权限能精确到
「单个文档 + 单个人」(实测), 这是 bitable 做不到的(它最细只到 base 级)。
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _rookie_sop_doc as _doc

_MAX_BLOCKS_PER_CALL = 50


def _parsed(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    d = payload.get("data")
    return d if isinstance(d, dict) else {}


async def create_doc(api: Any, title: str) -> dict[str, Any]:
    """建一个空文档, 返回 {ok, document_id, url}。"""
    res = _parsed(
        await api("POST", "/open-apis/docx/v1/documents", body_json=json.dumps({"title": title}, ensure_ascii=False))
    )
    if res.get("ok") is not True:
        return {"ok": False, "error": str(res.get("msg") or res.get("message") or "create document failed")}
    doc = _data(res).get("document")
    document_id = str((doc or {}).get("document_id") or "")
    if not document_id:
        return {"ok": False, "error": f"no document_id in response: {res}"}
    return {"ok": True, "document_id": document_id, "url": doc_url(document_id)}


def doc_url(document_id: str) -> str:
    return f"https://feishu.cn/docx/{document_id}"


async def append_blocks(api: Any, document_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """把块追加到文档根节点。飞书对单次子块数有上限, 故分批。"""
    if not blocks:
        return {"ok": True, "written": 0, "todo_block_ids": []}
    written = 0
    # 飞书在返回体里给出每个新建块的 block_id —— 收集 todo 块的 id, 用来建
    # block_id → item_id 映射, 于是文档正文不必写任何 item 标记(见 _rookie_sop_doc)。
    todo_block_ids: list[str] = []
    for start in range(0, len(blocks), _MAX_BLOCKS_PER_CALL):
        chunk = blocks[start : start + _MAX_BLOCKS_PER_CALL]
        res = _parsed(
            await api(
                "POST",
                f"/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                body_json=json.dumps({"children": chunk}, ensure_ascii=False),
            )
        )
        if res.get("ok") is not True:
            return {
                "ok": False,
                "error": str(res.get("msg") or res.get("message") or "append blocks failed"),
                "written": written,
                "todo_block_ids": todo_block_ids,
            }
        children = (_data(res).get("children") or res.get("children") or [])
        for child in children:
            if isinstance(child, dict) and child.get("block_type") == _doc.BLOCK_TODO:
                bid = str(child.get("block_id") or "")
                if bid:
                    todo_block_ids.append(bid)
        written += len(chunk)
    return {"ok": True, "written": written, "todo_block_ids": todo_block_ids}


async def read_blocks(api: Any, document_id: str) -> dict[str, Any]:
    """读回文档全部块 —— 翻页直到 has_more 为假, 同时防不动点循环。

    与 fetch_detail 同一套守卫: 上限 + 已见 token 集合, 否则服务端若一直回
    has_more=true 就会无限转(fire=tool 的同步会挂死整个回合)。
    """
    blocks: list[dict[str, Any]] = []
    seen: set[str] = {""}
    token = ""
    for _ in range(50):
        query: dict[str, Any] = {"page_size": 500}
        if token:
            query["page_token"] = token
        res = _parsed(
            await api(
                "GET",
                f"/open-apis/docx/v1/documents/{document_id}/blocks",
                query_json=json.dumps(query, ensure_ascii=False),
            )
        )
        if res.get("ok") is not True:
            return {"ok": False, "error": str(res.get("msg") or res.get("message") or "read blocks failed")}
        payload = _data(res) or res
        items = payload.get("items")
        if isinstance(items, list):
            blocks.extend(b for b in items if isinstance(b, dict))
        nxt = str(payload.get("page_token") or "")
        if not payload.get("has_more") or not nxt or nxt in seen:
            break
        seen.add(nxt)
        token = nxt
    else:
        return {"ok": True, "blocks": blocks, "truncated": True}
    return {"ok": True, "blocks": blocks}


async def grant_edit(api: Any, document_id: str, open_id: str) -> dict[str, Any]:
    """把文档授权给这个新人(edit, 他要能勾)。

    参数位置是飞书的坑: `type`(文件类型)必须在 query, 而 body 里的 `type` 是
    **成员类别**、只认 user/chat/department/group —— 端点表会在发请求前拦下写错的组合。
    """
    res = _parsed(
        await api(
            "POST",
            f"/open-apis/drive/v1/permissions/{document_id}/members",
            query_json=json.dumps({"type": "docx", "need_notification": "true"}, ensure_ascii=False),
            body_json=json.dumps(
                {"member_type": "openid", "member_id": open_id, "perm": "edit", "type": "user"},
                ensure_ascii=False,
            ),
        )
    )
    if res.get("ok") is not True:
        return {"ok": False, "error": str(res.get("msg") or res.get("message") or "grant failed")}
    return {"ok": True}


async def subscribe_changes(api: Any, document_id: str) -> dict[str, Any]:
    """订阅这份文档的变更事件 —— 勾选后由事件驱动同步, 不靠轮询。

    刻意为之: 事件只说「这个文档被编辑了」, 不说哪一项被勾。所以收到事件后仍要
    整份读回来对比。好处是只在真有变更时才干活: 10 个新人若用 5 分钟轮询是每天
    2880 次调用, 事件驱动只有实际勾选那几十次。
    """
    res = _parsed(
        await api(
            "POST",
            f"/open-apis/drive/v1/files/{document_id}/subscribe",
            query_json=json.dumps({"file_type": "docx"}, ensure_ascii=False),
        )
    )
    if res.get("ok") is not True:
        return {"ok": False, "error": str(res.get("msg") or res.get("message") or "subscribe failed")}
    return {"ok": True}


async def provision_doc(
    api: Any,
    *,
    open_id: str,
    name: str,
    rows: list[dict[str, Any]],
    sop_url: str = "",
) -> dict[str, Any]:
    """为一个新人备好详情页: 建文档 → 写清单 → 授权给他 → 订阅变更。

    每一步的失败都往上报, 不吞 —— 授权失败意味着新人打不开自己的清单,
    订阅失败意味着他勾了没人知道, 两者都不能当成功。
    """
    created = await create_doc(api, f"{name} · 入职清单")
    if created.get("ok") is not True:
        return created
    document_id = str(created["document_id"])

    blocks, slots = _doc.build_doc_blocks(rows, name=name, sop_url=sop_url)
    appended = await append_blocks(api, document_id, blocks)
    if appended.get("ok") is not True:
        return {"ok": False, "error": f"blocks: {appended.get('error')}", "document_id": document_id}

    # 按顺序配对: slots[i] 对应第 i 个 todo 块。数量不等说明飞书少建/多建了块,
    # 这时映射会错位, 宁可报错也不要把状态同步到错误的条目上。
    todo_ids = appended.get("todo_block_ids") or []
    if len(todo_ids) != len(slots):
        return {
            "ok": False,
            "error": f"todo block count mismatch: got {len(todo_ids)}, expected {len(slots)}",
            "document_id": document_id,
        }
    block_map = {bid: f"{item_id}:{role}" for bid, (item_id, role) in zip(todo_ids, slots, strict=True)}

    granted = await grant_edit(api, document_id, open_id)
    subscribed = await subscribe_changes(api, document_id)
    return {
        "ok": granted.get("ok") is True and subscribed.get("ok") is True,
        "document_id": document_id,
        "url": doc_url(document_id),
        "blocks_written": appended.get("written"),
        "block_map": block_map,
        "granted": granted.get("ok") is True,
        "grant_error": granted.get("error", ""),
        "subscribed": subscribed.get("ok") is True,
        "subscribe_error": subscribed.get("error", ""),
    }
