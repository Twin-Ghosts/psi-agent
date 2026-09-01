from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

# ruff: noqa: RUF001

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from _positive_negative_list.models import (  # noqa: E402  # ty: ignore[unresolved-import]
    CaseDraft,
    LedgerQuery,
    LedgerRecord,
)
from _positive_negative_list.validation import validate_case  # noqa: E402  # ty: ignore[unresolved-import]


def _negative_case() -> CaseDraft:
    return CaseDraft.from_mapping(
        {
            "writer_user_key": "ou_subject",
            "reporter_user_key": "ou_reporter",
            "subject_user_key": "ou_subject",
            "occurred_at": "2026-09-01",
            "observed_behavior": "已知延期后未同步风险",
            "context": "项目交付",
            "impact": "上下游等待",
            "evidence_sources": ["聊天记录"],
            "nature": "negative",
            "category": "迅速行动、及时反馈",
            "primary_rule_id": "pn-test-negative",
            "secondary_rule_ids": [],
            "rule_version": "6.0-shadow",
            "fact_summary": "XXX 已知延期后未及时同步, 导致上下游等待。",
            "agent_inference": "根据已提供聊天记录判断。",
            "correct_behavior": "发现延期风险后立即同步现状、影响和新时间。",
            "immediate_remedy": "现在补充同步并明确补救负责人和时间。",
            "prevention": "设置里程碑风险反馈节点。",
            "workflow": "ready_for_confirmation",
            "case_id": "case_test",
        }
    )


def test_bitable_role_permission_denial_is_not_reported_as_empty_success() -> None:
    feishu = importlib.import_module("_feishu_impl")
    response = SimpleNamespace(
        code=0,
        msg="RolePermNotAllow",
        raw=SimpleNamespace(content=b'{"code":0,"msg":"RolePermNotAllow","data":{}}', status_code=200),
    )

    result = feishu._resp_to_result(response)

    assert result["ok"] is False
    assert result["code"] == 0
    assert "permission" in result["message"].lower()


def test_public_source_reader_requests_only_columns_known_to_the_table(monkeypatch) -> None:
    reader = importlib.import_module("_positive_negative_list.reader")
    calls: list[dict[str, Any]] = []

    async def fake_search(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "records": [], "has_more": False, "page_token": ""}

    monkeypatch.setattr(reader._f, "search_bitable_records_impl", fake_search)
    client = reader.FeishuLedgerClient(
        "app",
        "table",
        {
            "case_id": "记录ID",
            "nature": "正负面归属",
            "subject_user_key": "员工姓名",
            "reporter_user_key": "填写人",
            "occurred_at": "记录日期",
            "fact_summary": "事件描述",
        },
        strict_field_names=True,
    )

    asyncio.run(client.list_records(LedgerQuery(), "ou_reader"))
    requested = json.loads(calls[0]["field_names"])
    assert requested == ["员工姓名", "填写人", "正负面归属", "记录日期", "事件描述"]


def test_feishu_rich_text_is_normalized_for_analysis() -> None:
    record = LedgerRecord.from_mapping(
        {
            "record_id": "rec_1",
            "fields": {
                "事件描述": [{"text": "未更新 TODO", "type": "text"}],
                "正负面归属": "负面清单",
                "员工姓名": [{"id": "ou_subject", "name": "XXX"}],
                "记录日期": 1786896000000,
                "填写人": [{"id": "ou_reporter", "name": "报告人"}],
            },
        },
        field_names={
            "case_id": "记录ID",
            "nature": "正负面归属",
            "subject_user_key": "员工姓名",
            "reporter_user_key": "填写人",
            "occurred_at": "记录日期",
            "fact_summary": "事件描述",
        },
    )
    assert record.fact_summary == "未更新 TODO"
    assert record.subject_user_key == "ou_subject"
    assert record.nature == "negative"


def test_reader_does_not_return_verbose_raw_feishu_fields() -> None:
    reader = importlib.import_module("_positive_negative_list.reader")

    class FakeClient:
        field_names: ClassVar[dict[str, str]] = {
            "nature": "正负面归属",
            "subject_user_key": "员工姓名",
            "reporter_user_key": "填写人",
            "occurred_at": "记录日期",
            "fact_summary": "事件描述",
        }

        async def list_records(self, query, user_key):
            return {
                "ok": True,
                "records": [
                    {
                        "record_id": "rec_1",
                        "fields": {
                            "事件描述": [{"text": "未更新 TODO", "type": "text"}],
                            "正负面归属": "负面清单",
                            "员工姓名": [
                                {
                                    "id": "ou_subject",
                                    "name": "XXX",
                                    "avatar_url": "https://example.invalid/avatar",
                                    "email": "subject@example.invalid",
                                }
                            ],
                            "记录日期": 1786896000000,
                            "填写人": [{"id": "ou_reporter", "name": "报告人"}],
                        },
                    }
                ],
                "has_more": False,
                "page_token": "",
            }

    result = asyncio.run(reader.read_records(FakeClient(), LedgerQuery(), "ou_reader"))

    assert result["ok"] is True
    record = result["records"][0]
    assert record["subject_user_key"] == "ou_subject"
    assert record["fact_summary"] == "未更新 TODO"
    assert "fields" not in record
    assert "avatar_url" not in json.dumps(result, ensure_ascii=False)
    assert "subject@example.invalid" not in json.dumps(result, ensure_ascii=False)


def test_analyze_tool_returns_user_facing_summary_without_internal_metadata(monkeypatch) -> None:
    analyze = importlib.import_module("positive_negative_case_analyze")

    class FakeAdapter:
        async def list_records(self, query, user_key):
            return {
                "ok": True,
                "records": [
                    {
                        "record_id": "rec_1",
                        "fields": {
                            "事件描述": "未及时同步延期风险",
                            "正负面归属": "负面清单",
                            "员工姓名": "XXX",
                            "记录日期": "2026-08-28",
                            "填写人": "报告人",
                        },
                    },
                    {
                        "record_id": "rec_2",
                        "fields": {
                            "事件描述": "主动补位完成闭环",
                            "正负面归属": "正面清单",
                            "员工姓名": "XXX",
                            "记录日期": "2026-08-29",
                            "填写人": "报告人",
                        },
                    },
                ],
                "has_more": False,
                "page_token": "",
            }

    monkeypatch.setattr(analyze.runtime, "configured_read_table_adapter", lambda: FakeAdapter())
    payload = json.loads(asyncio.run(analyze.positive_negative_case_analyze(user_key="ou_reader")))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["ok"] is True
    assert payload["摘要"]["记录总数"] == 2
    assert payload["摘要"]["正面记录数"] == 1
    assert payload["摘要"]["负面记录数"] == 1
    assert payload["摘要"]["证据来源未填写"] == 2
    assert payload["摘要"]["负面记录待复盘"] == 1
    assert "evidence_sources" not in serialized
    assert "review_status" not in serialized
    assert "has_more" not in serialized
    assert "page_token" not in serialized
    assert "6.0-shadow" not in serialized
    assert "pn-" not in serialized


def test_read_tool_returns_chinese_record_fields_without_internal_metadata(monkeypatch) -> None:
    read = importlib.import_module("positive_negative_case_read")

    class FakeClient:
        field_names: ClassVar[dict[str, str]] = {
            "nature": "正负面归属",
            "subject_user_key": "员工姓名",
            "reporter_user_key": "填写人",
            "occurred_at": "记录日期",
            "fact_summary": "事件描述",
        }

        async def list_records(self, query, user_key):
            return {
                "ok": True,
                "records": [
                    {
                        "record_id": "rec_1",
                        "fields": {
                            "事件描述": "未及时同步延期风险",
                            "正负面归属": "负面清单",
                            "员工姓名": "XXX",
                            "记录日期": "2026-08-28",
                            "填写人": "报告人",
                        },
                    }
                ],
                "has_more": False,
                "page_token": "",
            }

    class FakeAdapter:
        _client = FakeClient()

    monkeypatch.setattr(read.runtime, "configured_read_table_adapter", lambda: FakeAdapter())
    payload = json.loads(asyncio.run(read.positive_negative_case_read(user_key="ou_reader")))
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["ok"] is True
    record = payload["记录"][0]
    assert record["记录编号"] == "rec_1"
    assert record["行为性质"] == "负面清单"
    assert "evidence_sources" not in serialized
    assert "review_status" not in serialized
    assert "has_more" not in serialized
    assert "page_token" not in serialized
    assert "case_id" not in serialized


def test_robot_test_target_preflight_does_not_need_extra_view_config(monkeypatch) -> None:
    runtime = importlib.import_module("_positive_negative_list.runtime")
    monkeypatch.setattr(runtime, "_TEST_APP_TOKEN", "test_app")
    monkeypatch.setattr(runtime, "_TEST_TABLE_ID", "test_table")
    monkeypatch.setattr(runtime, "_TEST_VIEW_ID", "default_view")
    config = {
        "write_target": {
            "mode": "existing_columns",
            "field_names": dict(runtime._TEST_FIELD_NAMES),
        }
    }
    client = runtime.ConfiguredTableClient("test_app", "test_table", config)

    async def fake_list_fields(*args, **kwargs):
        type_by_name = {
            "事件描述": 1,
            "正负面归属": 3,
            "员工姓名": 11,
            "记录日期": 5,
            "备注": 1,
            "填写人": 11,
        }
        return {
            "ok": True,
            "fields": [
                {
                    "field_id": f"field_{index}",
                    "name": name,
                    "type": field_type,
                    "property": (
                        {"options": [{"name": option} for option in ("正面清单", "负面清单", "中性", "证据不足")]}
                        if name == "正负面归属"
                        else {}
                    ),
                }
                for index, (name, field_type) in enumerate(type_by_name.items())
            ],
        }

    monkeypatch.setattr(runtime._f, "list_bitable_fields_impl", fake_list_fields)
    result = asyncio.run(client.preflight("ou_writer"))
    assert result.ok is True
    assert result.schema is not None
    assert result.schema.view_purposes == {"default_view": "public_ledger"}


def test_writer_may_be_subject_when_reporter_is_a_different_person() -> None:
    assert validate_case(_negative_case()) == ()


def test_self_reported_case_is_valid_for_private_chat() -> None:
    case = CaseDraft.from_mapping(
        _negative_case().to_mapping()
        | {
            "reporter_user_key": "ou_subject",
            "writer_user_key": "ou_subject",
        }
    )

    assert validate_case(case) == ()


def test_confirmed_negative_case_starts_private_review_and_notice_guidance(tmp_path) -> None:
    confirm = importlib.import_module("positive_negative_list_confirm")
    notifications = importlib.import_module("_positive_negative_list.notifications")
    reviews = importlib.import_module("_positive_negative_list.reviews")
    case = _negative_case()

    count = confirm._start_private_reviews(case, "rec_test", tmp_path)

    active = reviews.find_active_reviews(tmp_path, "ou_subject")
    assert count == 1
    assert len(active) == 1
    assert active[0].record_id == "rec_test"
    notice = notifications._notice_text(case, "https://feishu.cn/base/test")
    assert "客观原因" in notice
    assert "补足动作" in notice
    assert "防止再犯" in notice
    assert "不写回表格" in notice
    assert "行为性质：负面行为" in notice


def test_self_reported_negative_case_starts_private_review_without_duplicate_notice(tmp_path) -> None:
    confirm = importlib.import_module("positive_negative_list_confirm")
    reviews = importlib.import_module("_positive_negative_list.reviews")
    case = CaseDraft.from_mapping(
        _negative_case().to_mapping() | {"reporter_user_key": "ou_subject", "writer_user_key": "ou_subject"}
    )

    count = confirm._start_private_reviews(case, "rec_self", tmp_path)

    active = reviews.find_active_reviews(tmp_path, "ou_subject")
    assert count == 1
    assert len(active) == 1
    assert active[0].record_id == "rec_self"


def test_self_reported_negative_case_sends_review_prompt(monkeypatch) -> None:
    confirm = importlib.import_module("positive_negative_list_confirm")
    reviews = importlib.import_module("_positive_negative_list.reviews")
    case = CaseDraft.from_mapping(
        _negative_case().to_mapping() | {"reporter_user_key": "ou_subject", "writer_user_key": "ou_subject"}
    )
    sent: list[tuple[str, str, str]] = []

    async def fake_send(receive_id: str, text: str, receive_id_type: str):
        sent.append((receive_id, text, receive_id_type))
        return {"ok": True, "message_id": "msg_review"}

    monkeypatch.setattr(reviews, "send_message_impl", fake_send)
    status = asyncio.run(confirm._send_self_review_prompts(case, "rec_self"))

    assert status == "notification_sent"
    assert len(sent) == 1
    assert sent[0][0] == "ou_subject"
    assert "客观原因" in sent[0][1]


def test_confirmation_card_uses_display_name_but_keeps_open_id_in_case(monkeypatch) -> None:
    positive_negative = importlib.import_module("positive_negative_list")

    async def fake_get_users_batch(user_ids: str, user_id_type: str = "open_id"):
        assert user_ids == "ou_subject"
        assert user_id_type == "open_id"
        return {"ok": True, "users": [{"open_id": "ou_subject", "name": "王炜博"}]}

    monkeypatch.setattr(positive_negative._f, "get_users_batch_impl", fake_get_users_batch)
    case = _negative_case()

    card = asyncio.run(positive_negative._confirmation_card(case, "digest_test"))
    content = card["elements"][0]["content"]

    assert "涉事人**：王炜博" in content
    assert "ou_subject" not in content
    assert case.subject_user_key == "ou_subject"


def test_prepare_error_lists_legal_case_field_names(monkeypatch) -> None:
    positive_negative = importlib.import_module("positive_negative_list")

    result = asyncio.run(
        positive_negative.positive_negative_case_prepare(
            json.dumps({"员工姓名": "王炜博", "行为事实": "未及时同步"}),
            user_key="ou_writer",
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is False
    assert "allowed_case_fields" in payload
    assert "subject_user_key" in payload["allowed_case_fields"]
    assert "fact_summary" in payload["allowed_case_fields"]


def test_test_table_request_keeps_formal_six_column_order() -> None:
    runtime = importlib.import_module("_positive_negative_list.runtime")

    body = runtime._create_table_request("app_test").body
    fields = body["table"]["fields"]

    assert [field["field_name"] for field in fields] == [
        "事件描述",
        "正负面归属",
        "员工姓名",
        "记录日期",
        "备注",
        "填写人",
    ]


def test_existing_columns_store_human_note_and_formal_nature_label() -> None:
    runtime = importlib.import_module("_positive_negative_list.runtime")
    preflight = importlib.import_module("_positive_negative_list.preflight")
    case = _negative_case()
    fields = [
        {"field_id": "f_desc", "field_name": "事件描述", "type": 1},
        {
            "field_id": "f_nature",
            "field_name": "正负面归属",
            "type": 3,
            "property": {"options": [{"name": "正面清单"}, {"name": "负面清单"}]},
        },
        {"field_id": "f_subject", "field_name": "员工姓名", "type": 11},
        {"field_id": "f_date", "field_name": "记录日期", "type": 5},
        {"field_id": "f_note", "field_name": "备注", "type": 1},
        {"field_id": "f_reporter", "field_name": "填写人", "type": 11},
    ]
    client = runtime.ConfiguredTableClient(
        "app_test",
        "table_test",
        {"write_target": {"mode": "existing_columns", "field_names": runtime._TEST_FIELD_NAMES}},
    )
    schema_result = preflight.validate_table_schema(
        fields,
        {
            "nature": {"field_id": "f_nature", "field_name": "正负面归属", "type": 3},
            "subject_user_key": {"field_id": "f_subject", "field_name": "员工姓名", "type": 11},
            "fact_summary": {"field_id": "f_desc", "field_name": "事件描述", "type": 1},
            "occurred_at": {"field_id": "f_date", "field_name": "记录日期", "type": 5},
            "reporter_user_key": {"field_id": "f_reporter", "field_name": "填写人", "type": 11},
            "source_key": {"field_id": "f_note", "field_name": "备注", "type": 1},
            "canonical_incident_id": {"field_id": "f_note", "field_name": "备注", "type": 1},
            "cross_source_fingerprint": {"field_id": "f_note", "field_name": "备注", "type": 1},
        },
        {"nature": {"field_id": "f_nature", "options": frozenset({"正面清单", "负面清单"})}},
        app_token="app_test",
        target_table_id="table_test",
        candidate_table_ids=("table_test",),
        view_purposes={"table_test": "public_ledger"},
        can_create_records=True,
        notification_user_key="ou_writer",
        notification_identity_provenance="trusted_feishu_context",
        allow_deduplication_aliases=True,
    )
    assert schema_result.ok is True
    encoded = client.build_existing_case_fields(case, schema_result.schema)

    assert encoded["f_desc"] == case.fact_summary
    assert encoded["f_nature"] == "负面清单"
    assert "分类：" in encoded["f_note"]
    assert "正确做法：" in encoded["f_note"]
    assert case.cross_source_fingerprint in encoded["f_note"]
