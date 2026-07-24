from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_protocol() -> ModuleType:
    path = Path(__file__).parents[2] / "examples" / "haitun-workspace" / "systems" / "supervisor_protocol.py"
    module = ModuleType("haitun_supervisor_protocol")
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def _valid_advice() -> dict[str, Any]:
    return {
        "classification": {
            "is_learning": True,
            "domain": "machine-learning",
            "topic": "overfitting",
            "confidence": 0.9,
        },
        "breakout": {
            "needed": True,
            "type": "broaden",
            "score": 0.8,
            "reason": "当前问题需要放回机器学习全局框架。",
            "directions": ["偏差与方差", "模型评估"],
            "evidence": ["连续聚焦局部定义"],
        },
        "latent_need": {
            "detected": True,
            "need": "建立领域框架",
            "missing_dimensions": ["方法之间的关系"],
            "confidence": 0.7,
        },
        "profile_shift": {
            "detected": True,
            "from": "入门",
            "to": "体系化理解",
            "evidence": ["开始追问机制"],
            "confidence": 0.8,
        },
        "response_strategy": {
            "answer_depth": "deep",
            "answer_scope": "framework",
            "goal_mode": "explain",
            "terminology": "explain_key_terms",
            "breakout_integration": "integrated_section",
            "instructions": ["先给结论, 再给框架"],
        },
        "diagnostics": {"source": "live"},
    }


def test_protocol_validation_repairs_and_bounds_values() -> None:
    protocol = _load_protocol()

    assert protocol.validate_advice(_valid_advice())["breakout"]["type"] == "broaden"
    clamped = protocol.validate_advice({"breakout": {"score": 4}, "diagnostics": {"source": "live"}})
    assert clamped["breakout"]["score"] == 1.0
    assert clamped["diagnostics"]["source"] == "repaired"
    directions = protocol.validate_advice({"breakout": {"directions": ["one", "two", "three", "four"]}})["breakout"][
        "directions"
    ]
    assert directions == ["one", "two", "three"]
    assert len(directions) == 3
    assert protocol.validate_advice("not a dict")["diagnostics"]["source"] == "unavailable"


def test_protocol_malformed_section_marks_live_payload_repaired() -> None:
    protocol = _load_protocol()
    raw = _valid_advice()
    raw["user_state"] = "malformed"

    assert protocol.validate_advice(raw)["diagnostics"]["source"] == "repaired"


def test_protocol_extracts_plain_fenced_and_embedded_json() -> None:
    protocol = _load_protocol()
    payload = {"classification": {"is_learning": True}, "note": "含有 {括号}"}

    assert protocol.extract_json_object(json.dumps(payload, ensure_ascii=False)) == payload
    assert protocol.extract_json_object(f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```") == payload
    assert protocol.extract_json_object(f"分析如下: {json.dumps(payload, ensure_ascii=False)} 后续文字") == payload
    assert protocol.extract_json_object("not {valid json}") is None


def test_protocol_invalid_enums_and_contradictory_breakout_are_disabled() -> None:
    protocol = _load_protocol()
    raw = _valid_advice()
    raw["breakout"] = {
        "needed": False,
        "type": "teleport",
        "score": 0.9,
        "reason": "理由",
        "directions": ["方向"],
    }
    raw["response_strategy"]["answer_depth"] = "infinite"

    advice = protocol.validate_advice(raw)

    assert advice["breakout"]["needed"] is False
    assert advice["breakout"]["type"] == "none"
    assert advice["response_strategy"]["answer_depth"] == "balanced"
    assert advice["diagnostics"]["source"] == "repaired"


def test_protocol_rendering_is_concise_and_safe() -> None:
    protocol = _load_protocol()
    prompt = protocol.render_advice_prompt(protocol.validate_advice(_valid_advice()))

    assert prompt.startswith("## 旁路监督建议")
    assert "machine-learning" in prompt
    assert "overfitting" in prompt
    assert "先回答用户当前问题。" in prompt
    assert "不要向用户提及副 Agent、监督评分或画像判断。" in prompt
    assert "不要强迫用户转换话题。" in prompt
    assert protocol.render_advice_prompt(protocol.empty_advice()) == ""
    non_learning = _valid_advice()
    non_learning["classification"]["is_learning"] = False
    assert protocol.render_advice_prompt(protocol.validate_advice(non_learning)) == ""
