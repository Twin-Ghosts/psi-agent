"""把 SOP 清单渲染成飞书文档的块, 以及把文档块读回成勾选状态 —— 纯逻辑, 不碰飞书。

刻意为之: 详情页用飞书文档的 todo 块(block_type 17), 而不是多维表格的表单视图。
两条路都实测过:
  - 表单视图: 能用 API 建, 但**不支持按人筛选**(加 filter_info 报 field validation
    failed), 而 bitable 的权限最细只到 base 级 —— 同一个 base 里的表, 能看就是全看。
    所以做不到「一人一份、只有自己能看自己那份」。
  - 文档: todo 块可建、done 状态可读回, 且**权限能精确到单个文档 + 单个人**
    (实测 POST /drive/v1/permissions/:doc/members 授 edit 返回 ok=True)。
    一人一份文档、只授权他本人, 隔离天然成立。

数据仍然以明细表为唯一事实来源: 文档只是新人勾选的界面, 勾完由
rookie_sop_sync_doc 把 done 状态同步回表, 所以 HR 日报的数据源完全不用改。
"""

from __future__ import annotations

# ruff: noqa: RUF001, RUF003
from datetime import date
from typing import Any

# 飞书文档块类型(见 _feishu_impl 的块类型表)
BLOCK_TEXT = 2
BLOCK_HEADING2 = 4
BLOCK_TODO = 17
BLOCK_DIVIDER = 22

# 文档里每个 todo 块的文字前缀 —— 同步时靠它把块认回条目。
# 刻意用不可见的分隔思路: 条目名后跟一个「〔item_id〕」标记, 新人看得见但不碍事,
# 因为 done 状态是按块顺序对不上的(新人可能自己加行、删行、重排)。
_ID_OPEN = "〔"
_ID_CLOSE = "〕"


def _text_run(content: str, bold: bool = False) -> dict[str, Any]:
    style: dict[str, Any] = {"bold": True} if bold else {}
    return {"text_run": {"content": content, "text_element_style": style}}


def _link_run(url: str) -> dict[str, Any]:
    """一段可点的链接。飞书文档的 text_run 用 text_element_style.link.url 承载超链接。"""
    return {"text_run": {"content": url, "text_element_style": {"link": {"url": url}}}}


def item_marker(item_id: str) -> str:
    """条目在文档里的可识别标记。同步时按它匹配, 不依赖块顺序。"""
    return f"{_ID_OPEN}{item_id}{_ID_CLOSE}"


def parse_item_id(text: str) -> str:
    """从一行 todo 文字里取回 item_id; 认不出返回空串。"""
    if _ID_OPEN not in text or _ID_CLOSE not in text:
        return ""
    start = text.rindex(_ID_OPEN) + len(_ID_OPEN)
    end = text.rindex(_ID_CLOSE)
    return text[start:end].strip() if end > start else ""


def build_doc_blocks(
    rows: list[dict[str, Any]],
    *,
    name: str,
    today: date | None = None,
    sop_url: str = "",
) -> list[dict[str, Any]]:
    """把明细行渲染成文档块: 按模块分节, 每项一个 todo 块。

    done 状态直接来自明细表 —— 文档是表的投影, 重建文档时已完成的项照旧是勾上的。
    """
    blocks: list[dict[str, Any]] = [
        {
            "block_type": BLOCK_TEXT,
            "text": {
                "elements": [
                    _text_run(f"{name} 的入职清单", bold=True),
                    _text_run("　逐项打勾即可，勾完会自动同步给 HR，无需另行提交。"),
                ],
                "style": {},
            },
        }
    ]
    if sop_url.strip():
        blocks.append(
            {
                "block_type": BLOCK_TEXT,
                "text": {"elements": [_text_run(f"完整 SOP: {sop_url.strip()}")], "style": {}},
            }
        )

    modules: list[str] = []
    for row in rows:
        module = str(row.get("模块") or "")
        if module and module not in modules:
            modules.append(module)

    for module in modules:
        module_rows = [r for r in rows if str(r.get("模块") or "") == module]
        done_n = sum(1 for r in module_rows if str(r.get("状态") or "") == "已完成")
        blocks.append({"block_type": BLOCK_DIVIDER, "divider": {}})
        blocks.append(
            {
                "block_type": BLOCK_HEADING2,
                "heading2": {
                    "elements": [_text_run(f"{module}　{done_n}/{len(module_rows)}")],
                    "style": {},
                },
            }
        )
        for row in module_rows:
            item_id = _item_id_of(row)
            title = str(row.get("项") or "").strip()
            acceptance = str(row.get("验收标准") or "").strip()
            status = str(row.get("状态") or "")
            # 不适用的项不给 todo 框 —— 勾它没有意义
            if status == "不适用":
                blocks.append(
                    {
                        "block_type": BLOCK_TEXT,
                        "text": {
                            "elements": [_text_run(f"（不适用）{title}")],
                            "style": {},
                        },
                    }
                )
                continue
            url = str(row.get("必读链接") or "").strip()
            if url:
                # 必读材料: 先给一行可点的链接, 再给一个「我已阅读并理解」的勾选框。
                # 刻意不用笼统的「完成」—— 阅读类的验收就是「读过并理解」, 说清楚
                # 要确认的是什么, 比让人对着一个「完成」猜要好。
                blocks.append(
                    {
                        "block_type": BLOCK_TEXT,
                        "text": {
                            "elements": [
                                _text_run(f"📖 {title}", bold=True),
                                _text_run("　"),
                                _link_run(url),
                            ],
                            "style": {},
                        },
                    }
                )
                blocks.append(
                    {
                        "block_type": BLOCK_TODO,
                        "todo": {
                            "elements": [_text_run(f"我已阅读并理解　{item_marker(item_id)}")],
                            "style": {"done": status == "已完成"},
                        },
                    }
                )
                continue
            label = title
            if acceptance:
                label += f"　—　{acceptance}"
            label += f"　{item_marker(item_id)}"
            blocks.append(
                {
                    "block_type": BLOCK_TODO,
                    "todo": {
                        "elements": [_text_run(label)],
                        "style": {"done": status == "已完成"},
                    },
                }
            )
    return blocks


def _item_id_of(row: dict[str, Any]) -> str:
    key = str(row.get("记录键") or "")
    return key.rsplit(":", 1)[-1] if ":" in key else key


def read_doc_state(blocks: list[dict[str, Any]]) -> dict[str, bool]:
    """从文档块读回 {item_id: 是否勾上}。

    按 item_marker 匹配而不是按块顺序 —— 新人可能自己在文档里加行、删行、重排,
    靠顺序对齐会把状态写到错误的条目上。认不出 item_id 的块直接忽略
    (可能是新人自己加的笔记)。
    """
    state: dict[str, bool] = {}
    for block in blocks:
        if not isinstance(block, dict) or block.get("block_type") != BLOCK_TODO:
            continue
        todo = block.get("todo")
        if not isinstance(todo, dict):
            continue
        text = "".join(
            str((e.get("text_run") or {}).get("content") or "")
            for e in (todo.get("elements") or [])
            if isinstance(e, dict)
        )
        item_id = parse_item_id(text)
        if not item_id:
            continue
        state[item_id] = bool((todo.get("style") or {}).get("done"))
    return state


def diff_state(doc_state: dict[str, bool], rows: list[dict[str, Any]]) -> list[str]:
    """文档里勾上、而表里还没记完成的 item_id。

    刻意为之: 只认「未完成 → 勾上」这一个方向。反向(表里已完成、文档里被取消勾选)
    不做撤销 —— 已完成是既成事实, 让新人在文档里取消勾选就能抹掉记录, 会让 HR 日报
    的数据变得不可信; 真要撤销应当由人工改表。
    """
    by_id = {_item_id_of(r): r for r in rows}
    out: list[str] = []
    for item_id, done in doc_state.items():
        if not done:
            continue
        row = by_id.get(item_id)
        if row is None:
            continue
        if str(row.get("状态") or "") == "未完成":
            out.append(item_id)
    return out
