from __future__ import annotations

import json
from typing import Any

BREAKOUT_TYPES = {
    "none",
    "broaden",
    "deepen",
    "reframe",
    "cross_domain",
    "operationalize",
}
ADVICE_SOURCES = {"live", "repaired", "stale", "unavailable"}

_ANSWER_DEPTHS = {"concise", "balanced", "deep"}
_ANSWER_SCOPES = {"local", "framework", "cross_domain"}
_GOAL_MODES = {"explain", "compare", "decide", "execute", "plan"}
_TERMINOLOGY = {"explain_all", "explain_key_terms", "professional"}
_BREAKOUT_INTEGRATIONS = {
    "none",
    "light_footer",
    "integrated_section",
    "restructure_answer",
}


def empty_advice(*, source: str = "unavailable") -> dict[str, Any]:
    if source not in ADVICE_SOURCES:
        source = "unavailable"
    return {
        "schema_version": "1.0",
        "advice_id": "",
        "user_id_hash": "",
        "profile_id": "",
        "turn_index": 0,
        "classification": {
            "is_learning": False,
            "domain": "",
            "topic": "",
            "confidence": 0.0,
        },
        "user_state": {
            "depth": 0.0,
            "goal": 0.0,
            "familiarity": 0.0,
            "evidence": [],
        },
        "breakout": {
            "needed": False,
            "type": "none",
            "score": 0.0,
            "reason": "",
            "directions": [],
            "evidence": [],
        },
        "latent_need": {
            "detected": False,
            "need": "",
            "missing_dimensions": [],
            "confidence": 0.0,
        },
        "profile_shift": {
            "detected": False,
            "from": "",
            "to": "",
            "evidence": [],
            "confidence": 0.0,
        },
        "response_strategy": {
            "answer_depth": "balanced",
            "answer_scope": "local",
            "goal_mode": "explain",
            "terminology": "explain_key_terms",
            "breakout_integration": "none",
            "instructions": [],
        },
        "map_updates": {
            "proposed_map": None,
            "visited_nodes": [],
            "branch_additions": [],
        },
        "diagnostics": {"source": source, "evidence": []},
    }


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _bounded_text(value: object, *, limit: int = 500) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _bounded_strings(value: object, *, maximum: int = 5, limit: int = 240) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _bounded_text(item, limit=limit)
        if text:
            result.append(text)
        if len(result) == maximum:
            break
    return result


def _score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def _enum(value: object, allowed: set[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def validate_advice(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return empty_advice()

    diagnostics = _section(raw, "diagnostics")
    requested_source = diagnostics.get("source")
    source = requested_source if requested_source in ADVICE_SOURCES else "repaired"
    advice = empty_advice(source=source)

    for key, limit in (
        ("schema_version", 20),
        ("advice_id", 128),
        ("user_id_hash", 128),
        ("profile_id", 128),
    ):
        value = _bounded_text(raw.get(key), limit=limit)
        if value:
            advice[key] = value
    turn_index = raw.get("turn_index")
    if isinstance(turn_index, int) and not isinstance(turn_index, bool):
        advice["turn_index"] = max(0, turn_index)

    classification = _section(raw, "classification")
    advice["classification"] = {
        "is_learning": classification.get("is_learning") is True,
        "domain": _bounded_text(classification.get("domain"), limit=120),
        "topic": _bounded_text(classification.get("topic"), limit=160),
        "confidence": _score(classification.get("confidence")),
    }

    user_state = _section(raw, "user_state")
    advice["user_state"] = {
        "depth": _score(user_state.get("depth")),
        "goal": _score(user_state.get("goal")),
        "familiarity": _score(user_state.get("familiarity")),
        "evidence": _bounded_strings(user_state.get("evidence")),
    }

    breakout = _section(raw, "breakout")
    breakout_type = _enum(breakout.get("type"), BREAKOUT_TYPES, "none")
    directions = _bounded_strings(breakout.get("directions"), maximum=3)
    reason = _bounded_text(breakout.get("reason"))
    needed = breakout.get("needed") is True
    repaired = breakout.get("type") is not None and breakout.get("type") not in BREAKOUT_TYPES
    if not needed or breakout_type == "none" or not reason or not directions:
        repaired = repaired or needed or breakout_type != "none"
        needed = False
        breakout_type = "none"
    advice["breakout"] = {
        "needed": needed,
        "type": breakout_type,
        "score": _score(breakout.get("score")),
        "reason": reason,
        "directions": directions,
        "evidence": _bounded_strings(breakout.get("evidence")),
    }

    latent_need = _section(raw, "latent_need")
    latent_text = _bounded_text(latent_need.get("need"))
    latent_detected = latent_need.get("detected") is True and bool(latent_text)
    advice["latent_need"] = {
        "detected": latent_detected,
        "need": latent_text if latent_detected else "",
        "missing_dimensions": _bounded_strings(latent_need.get("missing_dimensions"), maximum=5),
        "confidence": _score(latent_need.get("confidence")),
    }

    profile_shift = _section(raw, "profile_shift")
    shift_from = _bounded_text(profile_shift.get("from"), limit=160)
    shift_to = _bounded_text(profile_shift.get("to"), limit=160)
    shift_detected = (
        profile_shift.get("detected") is True and bool(shift_from) and bool(shift_to) and shift_from != shift_to
    )
    advice["profile_shift"] = {
        "detected": shift_detected,
        "from": shift_from if shift_detected else "",
        "to": shift_to if shift_detected else "",
        "evidence": _bounded_strings(profile_shift.get("evidence")),
        "confidence": _score(profile_shift.get("confidence")),
    }

    strategy = _section(raw, "response_strategy")
    repaired = repaired or any(
        value is not None and value not in allowed
        for value, allowed in (
            (strategy.get("answer_depth"), _ANSWER_DEPTHS),
            (strategy.get("answer_scope"), _ANSWER_SCOPES),
            (strategy.get("goal_mode"), _GOAL_MODES),
            (strategy.get("terminology"), _TERMINOLOGY),
            (strategy.get("breakout_integration"), _BREAKOUT_INTEGRATIONS),
        )
    )
    advice["response_strategy"] = {
        "answer_depth": _enum(strategy.get("answer_depth"), _ANSWER_DEPTHS, "balanced"),
        "answer_scope": _enum(strategy.get("answer_scope"), _ANSWER_SCOPES, "local"),
        "goal_mode": _enum(strategy.get("goal_mode"), _GOAL_MODES, "explain"),
        "terminology": _enum(strategy.get("terminology"), _TERMINOLOGY, "explain_key_terms"),
        "breakout_integration": _enum(strategy.get("breakout_integration"), _BREAKOUT_INTEGRATIONS, "none"),
        "instructions": _bounded_strings(strategy.get("instructions"), maximum=5),
    }

    map_updates = _section(raw, "map_updates")
    proposed_map = map_updates.get("proposed_map")
    advice["map_updates"] = {
        "proposed_map": proposed_map if isinstance(proposed_map, dict) else None,
        "visited_nodes": _bounded_strings(map_updates.get("visited_nodes"), maximum=20),
        "branch_additions": [item for item in map_updates.get("branch_additions", [])[:10] if isinstance(item, dict)]
        if isinstance(map_updates.get("branch_additions"), list)
        else [],
    }
    if repaired and source == "live":
        source = "repaired"
    advice["diagnostics"] = {
        "source": source,
        "evidence": _bounded_strings(diagnostics.get("evidence")),
    }
    return advice


def render_advice_prompt(advice: dict[str, Any]) -> str:
    validated = validate_advice(advice)
    if validated["diagnostics"]["source"] == "unavailable" or not validated["classification"]["is_learning"]:
        return ""

    classification = validated["classification"]
    breakout = validated["breakout"]
    latent_need = validated["latent_need"]
    profile_shift = validated["profile_shift"]
    strategy = validated["response_strategy"]
    lines = ["## 旁路监督建议"]
    if classification["domain"] or classification["topic"]:
        lines.append(f"- 当前领域/主题: {classification['domain'] or '未定'} / {classification['topic'] or '未定'}")
    if breakout["needed"]:
        lines.append(f"- 破圈方向 ({breakout['type']}): {breakout['reason']}")
        lines.append(f"- 可选延展: {'; '.join(breakout['directions'])}")
    if latent_need["detected"]:
        line = f"- 潜在需要: {latent_need['need']}"
        if latent_need["missing_dimensions"]:
            line += f"; 缺失维度: {'; '.join(latent_need['missing_dimensions'])}"
        lines.append(line)
    if profile_shift["detected"]:
        lines.append(f"- 学习阶段变化: {profile_shift['from']} → {profile_shift['to']}")
    lines.append(
        "- 回答策略: "
        f"深度={strategy['answer_depth']}, 范围={strategy['answer_scope']}, "
        f"目标={strategy['goal_mode']}, 术语={strategy['terminology']}, "
        f"破圈融入={strategy['breakout_integration']}。"
    )
    lines.extend(f"- {item}" for item in strategy["instructions"])
    lines.extend(
        (
            "- 先回答用户当前问题。",
            "- 不要向用户提及副 Agent、监督评分或画像判断。",
            "- 不要强迫用户转换话题。",
        )
    )
    return "\n".join(lines)
