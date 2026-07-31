from __future__ import annotations

from _assignment_tool_common import CLIENT, dumps_result, invalid_argument, parse_json_object


async def assignment_upsert(assignment_json: str) -> str:
    """Create or idempotently refresh a Fusion Memory organization work assignment.

    ``assignment_json`` must encode an object with this shape:

    - ``title``: non-empty task title.
    - ``state``: normally ``"assigned"`` after the assigner confirms delivery.
    - ``assigner``: object containing stable ``user_id``, ``display_name``, and the
      current ``feishu_open_id`` when known.
    - ``recipients``: array of participant objects with the same identity fields.
    - ``original_request``: the assigner's exact message or voice transcript. Do not
      rewrite it or mix Agent analysis into it.
    - ``context`` and ``expected_outcome``: confirmed background and expected result.
    - ``evidence_refs``: source objects such as ``{"uri": "https://..."}``.
    - ``gaps``, ``risks``, and ``action_items``: arrays of structured objects.
    - ``idempotency_key``: stable key reused for the same logical assignment.

    Optional fields include ``observers``, ``plan``, ``delivery_records``, and
    ``closure_reason``. Participants are matched by stable ``user_id``; a changed
    ``feishu_open_id`` is delivery metadata, not a new person or a new assignment.
    """
    assignment, error = parse_json_object(assignment_json, "assignment_json")
    if error is not None or assignment is None:
        return invalid_argument(error or "assignment_json must be a JSON object")
    result = await CLIENT.call_tool(
        "assignment_upsert",
        {"assignment": assignment},
        retryable=False,
    )
    return dumps_result(result)
