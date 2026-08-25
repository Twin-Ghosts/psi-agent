"""公司 TODO 体系的 per-mentor 台账 —— 幂等开通一张 base 并恰好授权三方。

为什么每个 mentor 一张独立 base,而不是一张大表加筛选视图:飞书的行级隔离要靠多维
表格的**自定义角色**,而自定义角色的前置是开高级权限,且 base 一旦放进 wiki 或被嵌进
文档就开不了(1254301)。筛选视图**不是权限** —— 拿到链接的人能看到整表。按 mentor 拆
独立 base,隔离退化成「文件给谁看」这件飞书原生就保证的事,不依赖任何高级特性。

为什么必须是工具而不是技能里的一段话:这里有三个动作必须一起成立 ——

1. **开通要幂等**:重复跑不能建出第二张台账。同名 base 在飞书里可以共存,靠模型「先搜
   一下」保证唯一性,搜漏一次就多一张,而两张台账的数据从此分叉,错误不会立刻暴露。
2. **授权要恰好一次**:mentor(编辑)、boss(只读)、机器人(编辑),多给一个人就是把
   别人的下属数据泄漏给他。
3. **table_id 必须解析而不是猜**:复制出来的 base 的 table_id 与模板不同,写错就是
   往一张不存在的表里写,或者写进占位表。

代价(已定案时接受的):base 数量等于 mentor 数量,跨 mentor 的全公司统计要逐个 base
读取后合并,不是一次查询。换来的是不依赖高级权限,也不存在「链接泄漏即全表泄漏」。
"""

from __future__ import annotations

import json
from typing import Any

import _feishu_impl as _core
from lark_channel.core.enum import AccessTokenType, HttpMethod
from lark_channel.core.model import BaseRequest

#: 台账 base 的命名模板。幂等判定就靠这个名字在目标文件夹里找,所以格式不能随意改。
_LEDGER_NAME_TEMPLATE = "TODO台账-{mentor}"

#: 台账里那张 todo 表的表名标记。复制模板后按标题找 table_id,而不是取「第一张表」
#: —— 模板里可能有说明表,顺序不保证。
_TODO_TABLE_MARKERS = ("todo", "台账")

#: base 名字的飞书限制:最长 100 字符,且不能含这些字符(违反返回 1254031)。
_NAME_FORBIDDEN = set("?/\\*:[]")
_NAME_MAX = 100

#: 一次列目录/列表的页大小。台账数量与 mentor 数同阶,一页足够;超了会翻页。
_PAGE_SIZE = 100

#: 授权矩阵:一个 mentor 台账恰好这三方可见,多一个都算泄漏。
#: perm 只有 view / edit / full_access 三档;full_access 是所有者级(能改权限、能删文件),
#: 台账不需要,所以最高只给 edit。
_PERM_EDIT = "edit"
_PERM_VIEW = "view"


def _build_app_create_request(name: str, folder_token: str) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.POST
    req.uri = "/open-apis/bitable/v1/apps"
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    body: dict[str, Any] = {"name": name}
    if folder_token:
        body["folder_token"] = folder_token
    req.body = body
    return req


def _build_app_copy_request(template_app_token: str, name: str, folder_token: str) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.POST
    req.uri = "/open-apis/bitable/v1/apps/:app_token/copy"
    req.paths["app_token"] = template_app_token
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    # without_content=true 只复制结构(空表、同样的列) —— 模板用法要的就是这个。
    req.body = {"name": name, "without_content": True, **({"folder_token": folder_token} if folder_token else {})}
    return req


def _build_list_tables_request(app_token: str, page_token: str = "") -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.GET
    req.uri = "/open-apis/bitable/v1/apps/:app_token/tables"
    req.paths["app_token"] = app_token
    req.add_query("page_size", _PAGE_SIZE)
    if page_token:
        req.add_query("page_token", page_token)
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    return req


def _build_list_folder_request(folder_token: str, page_token: str = "") -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.GET
    req.uri = "/open-apis/drive/v1/files"
    req.add_query("folder_token", folder_token)
    req.add_query("page_size", _PAGE_SIZE)
    if page_token:
        req.add_query("page_token", page_token)
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    return req


def _build_list_members_request(token: str, obj_type: str) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.GET
    req.uri = "/open-apis/drive/v1/permissions/:token/members"
    req.paths["token"] = token
    req.add_query("type", obj_type)
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    return req


def _build_add_member_request(token: str, obj_type: str, open_id: str, perm: str) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.POST
    req.uri = "/open-apis/drive/v1/permissions/:token/members"
    req.paths["token"] = token
    req.add_query("type", obj_type)
    req.add_query("need_notification", "false")
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    # member_type 是 "openid"(没有下划线),body 的 type 是「成员是哪一类」= user。
    # 这两个字段名近得容易串,串了飞书不会告诉你哪个错。
    req.body = {"member_type": "openid", "member_id": open_id, "perm": perm, "type": "user"}
    return req


def _validate_name(name: str) -> str | None:
    """base 名字的飞书限制。违反会返回 1254031,提前挡掉比读错误码快。"""
    if len(name) > _NAME_MAX:
        return f"台账名字 {len(name)} 字符超过飞书上限 {_NAME_MAX};把 mentor_name 写短一点。"
    bad = sorted(_NAME_FORBIDDEN & set(name))
    if bad:
        return f"台账名字不能含 {' '.join(bad)} (飞书返回 1254031);mentor_name 里有这些字符。"
    return None


def _data(res: dict[str, Any]) -> dict[str, Any]:
    """取 ``_invoke`` 结果里的 data 字典;形状不对时给空字典而不是 None。"""
    data = res.get("data")
    return data if isinstance(data, dict) else {}


async def _find_existing_ledger(name: str, folder_token: str, user_key: str) -> tuple[str, str]:
    """在目标文件夹里按名字找已有台账。返回 (app_token, error)。

    这是幂等的支点:找到就复用,不建第二张。翻页翻到底才敢说「没有」—— 半页就下结论
    等于在 mentor 多的时候随机多建台账。
    """
    page_token = ""
    while True:
        request = _build_list_folder_request(folder_token, page_token)
        res = await _core._invoke(request, user_key=user_key, prefer="user")
        if not res.get("ok"):
            return "", str(res.get("error") or res.get("message") or "列目录失败")
        data = _data(res)
        for item in data.get("files") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "") == name and str(item.get("type") or "") == "bitable":
                return str(item.get("token") or ""), ""
        page_token = str(data.get("next_page_token") or "")
        if not data.get("has_more") or not page_token:
            return "", ""


async def _resolve_todo_table(app_token: str, table_name: str, user_key: str) -> tuple[str, str, str]:
    """解析台账里那张 todo 表的 table_id。返回 (table_id, title, error)。

    按标题找,不取「第一张表」:复制模板得到的表顺序不保证,模板里也可能有说明表。
    找不到时把实际表名列出来,而不是回一句「读不到」。
    """
    page_token = ""
    titles: list[str] = []
    wanted = table_name.strip().casefold()
    fallback: tuple[str, str] | None = None
    while True:
        res = await _core._invoke(_build_list_tables_request(app_token, page_token), user_key=user_key)
        if not res.get("ok"):
            return "", "", str(res.get("error") or res.get("message") or "列数据表失败")
        data = _data(res)
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            table_id = str(item.get("table_id") or "")
            title = str(item.get("name") or "")
            if not table_id:
                continue
            titles.append(title)
            low = title.casefold()
            if wanted:
                if low == wanted:
                    return table_id, title, ""
                if wanted in low and fallback is None:
                    fallback = (table_id, title)
            elif fallback is None and any(marker in low for marker in _TODO_TABLE_MARKERS):
                fallback = (table_id, title)
        page_token = str(data.get("page_token") or "")
        if not data.get("has_more") or not page_token:
            break
    if fallback:
        return fallback[0], fallback[1], ""
    hint = "、".join(titles) if titles else "(这个 base 里没有数据表)"
    if wanted:
        return "", "", f"台账里没有名为 {table_name!r} 的数据表。实际的表: {hint}"
    return "", "", f"台账里没找到 todo 表(表名含 todo/台账)。实际的表: {hint}"


async def _ensure_members(
    app_token: str, wanted: list[tuple[str, str, str]], user_key: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """把授权收敛到目标状态。返回 (granted, failed, unexpected)。

    先列现有协作者:已经对的不重复加(重复加会把 perm 改回去,也刷通知)。**只加不减** ——
    撤权是不可逆的破坏性动作,发现多余协作者时报告出来交人裁决,不自己删。
    """
    existing: dict[str, str] = {}
    listed = await _core._invoke(_build_list_members_request(app_token, "bitable"), user_key=user_key)
    if listed.get("ok"):
        for member in _data(listed).get("items") or []:
            if isinstance(member, dict):
                member_id = str(member.get("member_id") or "")
                if member_id:
                    existing[member_id] = str(member.get("perm") or "")

    granted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for role, open_id, perm in wanted:
        if existing.get(open_id) == perm:
            granted.append({"role": role, "open_id": open_id, "perm": perm, "already": True})
            continue
        res = await _core._invoke(
            _build_add_member_request(app_token, "bitable", open_id, perm), user_key=user_key, prefer="user"
        )
        if res.get("ok"):
            granted.append({"role": role, "open_id": open_id, "perm": perm, "already": False})
        else:
            failed.append(
                {
                    "role": role,
                    "open_id": open_id,
                    "perm": perm,
                    "error": str(res.get("error") or res.get("message") or "加协作者失败"),
                }
            )
    unexpected = sorted(set(existing) - {open_id for _, open_id, _ in wanted})
    return granted, failed, unexpected


async def ensure_mentor_ledger_impl(
    mentor_open_id: str,
    mentor_name: str,
    folder_token: str,
    boss_open_id: str = "",
    template_app_token: str = "",
    table_name: str = "",
    user_key: str = "",
) -> dict[str, Any]:
    """幂等开通一个 mentor 的 TODO 台账 base,并把可见范围收敛到该 mentor 与 boss。

    幂等靠「在 ``folder_token`` 里按名字 ``TODO台账-<mentor_name>`` 找」实现:找到就复用,
    连跑两次只会有一张台账。``template_app_token`` 给了就复制模板(``without_content``,
    只带结构),没给就建一张空 base —— 后者建出来只有一个占位索引列,列要另外补,所以
    生产路径应当维护一份模板。

    返回 ``created`` 标明这次是新建还是复用,``members`` 里逐个报告授权结果。
    ``unexpected_members`` 列出目标三方之外的既有协作者:**本工具只加不减**,撤权是不可
    逆动作,交人裁决。
    """
    mentor = mentor_open_id.strip()
    if not mentor:
        return _core._error("mentor_open_id is required (the mentor's ou_... open_id).")
    name_part = mentor_name.strip()
    if not name_part:
        return _core._error("mentor_name is required (it goes into the ledger's name).")
    folder = folder_token.strip()
    if not folder:
        return _core._error("folder_token is required: the ledgers must live in one known folder to stay idempotent.")

    ledger_name = _LEDGER_NAME_TEMPLATE.format(mentor=name_part)
    problem = _validate_name(ledger_name)
    if problem:
        return _core._error(problem)

    app_token, error = await _find_existing_ledger(ledger_name, folder, user_key)
    if error:
        return _core._error(f"查已有台账失败: {error}")

    created = False
    if not app_token:
        template = template_app_token.strip()
        request = (
            _build_app_copy_request(template, ledger_name, folder)
            if template
            else _build_app_create_request(ledger_name, folder)
        )
        res = await _core._invoke(request, user_key=user_key, prefer="user")
        if not res.get("ok"):
            return res
        payload = _data(res).get("app")
        payload = payload if isinstance(payload, dict) else _data(res)
        app_token = str(payload.get("app_token") or "")
        if not app_token:
            payload_text = json.dumps(_data(res), ensure_ascii=False)
            return _core._error(f"台账建出来了但返回里没有 app_token,无法继续: {payload_text}")
        created = True

    table_id, table_title, table_error = await _resolve_todo_table(app_token, table_name, user_key)

    wanted = [("mentor", mentor, _PERM_EDIT)]
    boss = boss_open_id.strip()
    if boss and boss != mentor:
        wanted.append(("boss", boss, _PERM_VIEW))
    granted, failed, unexpected = await _ensure_members(app_token, wanted, user_key)

    return {
        "ok": True,
        "app_token": app_token,
        "url": f"https://feishu.cn/base/{app_token}",
        "name": ledger_name,
        "created": created,
        "reused": not created,
        "from_template": bool(template_app_token.strip()) if created else False,
        "table_id": table_id,
        "table_name": table_title,
        **({"table_error": table_error} if table_error else {}),
        "members": granted,
        "members_failed": failed,
        "unexpected_members": unexpected,
    }
