import json
import re
from typing import Any, cast

from psi_agent.gateway._openapi import OPENAPI_SPEC, build_openapi_spec
from psi_agent.gateway._openapi_core import CORE_PATHS, CORE_RESPONSES, CORE_SCHEMAS
from psi_agent.gateway.desktop._openapi import DESKTOP_PATHS
from psi_agent.gateway.feishu._openapi import FEISHU_PATHS, FEISHU_SCHEMAS


def test_openapi_router_contract_uses_current_fields_only() -> None:
    spec = cast(dict[str, Any], OPENAPI_SPEC)
    paths = spec["paths"]
    schemas = spec["components"]["schemas"]

    assert {"post", "get"} <= set(paths["/routers"])
    assert "delete" in paths["/routers/{router_id}"]
    properties = schemas["RouterCreateRequest"]["properties"]
    assert properties["mode"]["enum"] == ["routing", "aggregation", "fallback"]
    assert properties["router_ai_id"]["nullable"] is True
    assert properties["router_timeout"]["nullable"] is True
    assert properties["target_timeout"]["nullable"] is True
    assert properties["max_context_chars"]["minimum"] == 1
    assert "default_ai_id" not in properties
    assert "max_context_length" not in properties
    upstream = schemas["RouterUpstreamInfo"]
    assert upstream["required"] == ["backend_type", "backend_id", "description"]
    assert upstream["properties"]["backend_type"]["enum"] == ["ai", "router"]
    assert schemas["RouterCreateRequest"]["oneOf"] == [
        {
            "properties": {
                "mode": {"enum": ["fallback"]},
                "router_ai_id": {"enum": [None]},
            }
        },
        {
            "properties": {
                "mode": {"enum": ["routing", "aggregation"]},
                "router_ai_id": {"type": "string", "minLength": 1},
            }
        },
    ]
    assert "409" in paths["/routers/{router_id}"]["delete"]["responses"]


def test_three_fragments_partition_the_full_spec() -> None:
    """三份片段的 path key 并集 == 完整 spec, 且互不重叠。

    刻意**不做**字节比对 —— 一份 spec 拆成三份之后不可能字节相同。
    """
    union = set(CORE_PATHS) | set(DESKTOP_PATHS) | set(FEISHU_PATHS)
    assert union == set(OPENAPI_SPEC["paths"])
    assert len(CORE_PATHS) + len(DESKTOP_PATHS) + len(FEISHU_PATHS) == len(union)
    assert set(CORE_SCHEMAS) | set(FEISHU_SCHEMAS) == set(OPENAPI_SPEC["components"]["schemas"])
    assert set(CORE_RESPONSES) == set(OPENAPI_SPEC["components"]["responses"])


def test_fragments_own_only_their_own_prefixes() -> None:
    """归属按 path 前缀可判: 产品前缀不许出现在公共片段里, 反之亦然。"""
    assert all(k.startswith(("/ui/", "/workspace/")) for k in DESKTOP_PATHS)
    assert all(k.startswith(("/feishu/", "/oauth/")) for k in FEISHU_PATHS)
    assert not any(k.startswith(("/ui/", "/workspace/", "/feishu/", "/oauth/")) for k in CORE_PATHS)
    # /oauth/* 归 ToB: 取件方全在 agents/feishu/tools 一侧, ToC 的登录不走 OAuth 跳转。
    assert {"/oauth/callback", "/oauth/code"} <= set(FEISHU_PATHS)


def test_product_lines_get_only_their_own_endpoints() -> None:
    tob = build_openapi_spec(desktop=False, feishu=True)
    toc = build_openapi_spec(desktop=True, feishu=False)

    assert not set(tob["paths"]) & set(DESKTOP_PATHS)
    assert set(FEISHU_PATHS) <= set(tob["paths"])
    assert not set(toc["paths"]) & set(FEISHU_PATHS)
    assert not [s for s in toc["components"]["schemas"] if s.startswith("Feishu")]
    # 公共那批两边都在, 每个 key 下 schema 与完整 spec 逐一相同。
    for spec in (tob, toc):
        assert set(CORE_PATHS) <= set(spec["paths"])
        assert all(v == OPENAPI_SPEC["paths"][k] for k, v in spec["paths"].items())


def test_every_assembled_spec_has_no_dangling_ref() -> None:
    """按开关裁掉片段后不许剩下解析不到的 $ref。"""
    for spec in (OPENAPI_SPEC, build_openapi_spec(desktop=False, feishu=True), build_openapi_spec(feishu=False)):
        components = cast(dict[str, Any], spec["components"])
        for section, name in set(re.findall(r'"#/components/(schemas|responses)/(\w+)"', json.dumps(spec))):
            assert name in components[section], f"dangling #/components/{section}/{name}"


def test_build_does_not_mutate_the_fragments() -> None:
    """装配走 dict 拷贝: 反复调用不许把片段本身改花。"""
    before = json.dumps(CORE_PATHS), json.dumps(CORE_SCHEMAS), json.dumps(FEISHU_PATHS)
    build_openapi_spec()["paths"]["/injected"] = {}
    build_openapi_spec()["components"]["schemas"]["Injected"] = {}
    assert (json.dumps(CORE_PATHS), json.dumps(CORE_SCHEMAS), json.dumps(FEISHU_PATHS)) == before
    assert "/injected" not in OPENAPI_SPEC["paths"]
