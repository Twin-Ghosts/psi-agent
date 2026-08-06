"""Feishu Calendar — list events, create events, per-person invites.

Split out of ``_feishu_impl.py`` by domain. The shared client/token layer stays
there: this module reaches it through ``_core`` so that everything patched on
``_feishu_impl`` (``_invoke``, ``_get_client``, ``_get_valid_uat``, ...) keeps
taking effect here. ``_feishu_impl`` re-exports every public name below, so tool
entrypoints keep importing it and nothing else has to change.
"""

from __future__ import annotations

import contextlib
from typing import Any

import _feishu_impl as _core
from lark_channel.core.enum import AccessTokenType, HttpMethod
from lark_channel.core.model import BaseRequest


def _fmt_ms(ms: Any) -> str:
    """Format a ms-epoch value (str/int) to 'YYYY-MM-DD HH:MM:SS', or '' if empty/0."""
    if not ms or str(ms) == "0":
        return ""
    import datetime  # noqa: PLC0415

    with contextlib.suppress(ValueError, OSError, OverflowError):
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    return str(ms)


# ── Calendar (日历) — create an event on the bot's primary calendar ───────────
#
# The bot creates events on its own primary calendar (auto-resolved). Bot/tenant
# token works (calendar:calendar), but the app must have bot ability enabled
# (else 190007). Attendees are added via a second call.

_primary_calendar_id: str | None = None


async def _get_primary_calendar_id() -> str | None:
    """Resolve (and cache) the bot's primary calendar_id, or None on failure."""
    global _primary_calendar_id
    if _primary_calendar_id:
        return _primary_calendar_id
    req = BaseRequest()
    req.http_method = HttpMethod.POST
    req.uri = "/open-apis/calendar/v4/calendars/primary"
    req.add_query("user_id_type", "open_id")
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    res = await _core._invoke(req)
    if not res["ok"]:
        return None
    data = res["data"] if isinstance(res["data"], dict) else {}
    for item in data.get("calendars", []) if isinstance(data.get("calendars"), list) else []:
        cal = item.get("calendar", {}) if isinstance(item, dict) else {}
        cid = cal.get("calendar_id", "")
        if cid:
            _primary_calendar_id = cid
            return cid
    return None


def _time_to_info(t: str, timezone: str) -> dict[str, str] | None:
    """Parse 'YYYY-MM-DD HH:MM' -> timed {timestamp, timezone}; 'YYYY-MM-DD' -> all-day {date, timezone}."""
    s = t.strip()
    if not s:
        return None
    import datetime  # noqa: PLC0415

    with contextlib.suppress(ValueError):
        dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")
        return {"timestamp": str(int(dt.timestamp())), "timezone": timezone}
    with contextlib.suppress(ValueError):
        datetime.datetime.strptime(s, "%Y-%m-%d")
        return {"date": s, "timezone": timezone}
    return None


def _build_create_event_request(calendar_id: str, body: dict[str, Any]) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.POST
    req.uri = "/open-apis/calendar/v4/calendars/:calendar_id/events"
    req.paths["calendar_id"] = calendar_id
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    req.body = body
    return req


def _build_add_attendees_request(calendar_id: str, event_id: str, open_ids: list[str]) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.POST
    req.uri = "/open-apis/calendar/v4/calendars/:calendar_id/events/:event_id/attendees"
    req.paths["calendar_id"] = calendar_id
    req.paths["event_id"] = event_id
    req.add_query("user_id_type", "open_id")
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    req.body = {"attendees": [{"type": "user", "user_id": oid} for oid in open_ids]}
    return req


async def create_event_impl(
    summary: str, start: str, end: str, description: str = "", attendees: str = "", timezone: str = "Asia/Shanghai"
) -> dict[str, Any]:
    """Create a calendar event on the bot's primary calendar, optionally adding attendees."""
    start_info = _time_to_info(start, timezone)
    end_info = _time_to_info(end, timezone)
    if start_info is None or end_info is None:
        return _core._error("start/end must be 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'.")
    calendar_id = await _core._get_primary_calendar_id()
    if not calendar_id:
        return _core._error(
            "Could not resolve the bot's primary calendar. Ensure bot ability is enabled and calendar scope granted."
        )
    body: dict[str, Any] = {"summary": summary, "start_time": start_info, "end_time": end_info}
    if description.strip():
        body["description"] = description
    res = await _core._invoke(_build_create_event_request(calendar_id, body))
    if not res["ok"]:
        return res
    data = res["data"] if isinstance(res["data"], dict) else {}
    event = data.get("event", {}) if isinstance(data.get("event"), dict) else {}
    event_id = event.get("event_id", "")
    result: dict[str, Any] = {
        "ok": True,
        "event_id": event_id,
        "calendar_id": calendar_id,
        "summary": event.get("summary", summary),
        "start": start,
        "end": end,
    }
    open_ids = [a.strip() for a in attendees.split(",") if a.strip()]
    if open_ids and event_id:
        att_res = await _core._invoke(_build_add_attendees_request(calendar_id, event_id, open_ids))
        if att_res["ok"]:
            result["attendees_added"] = open_ids
        else:
            result["attendee_warning"] = att_res.get("message", "failed to add attendees")
    return result


# ── Calendar (日历) — list events on a calendar over a time range ─────────────
#
# Read the schedule of a calendar (the bot's primary one by default) between two
# instants. Reading someone else's calendar needs the identity to have reader
# access to it; scope calendar:calendar or calendar:calendar.event:read.


def _ts_of(t: str, timezone: str) -> str | None:
    """Parse 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD' (00:00 that day) to a Unix-second string."""
    info = _time_to_info(t, timezone)
    if info is None:
        return None
    if "timestamp" in info:
        return info["timestamp"]
    import datetime  # noqa: PLC0415

    with contextlib.suppress(ValueError):
        dt = datetime.datetime.strptime(info["date"], "%Y-%m-%d")
        return str(int(dt.timestamp()))
    return None


def _build_list_events_request(
    calendar_id: str, start_ts: str, end_ts: str, page_size: int, page_token: str
) -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.GET
    req.uri = "/open-apis/calendar/v4/calendars/:calendar_id/events"
    req.paths["calendar_id"] = calendar_id
    req.add_query("start_time", start_ts)
    req.add_query("end_time", end_ts)
    req.add_query("page_size", page_size)
    req.add_query("user_id_type", "open_id")
    if page_token:
        req.add_query("page_token", page_token)
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    return req


def _event_time_str(t: Any) -> str:
    """Normalize a calendar event start/end object to a readable string."""
    if not isinstance(t, dict):
        return ""
    if t.get("timestamp"):
        return _fmt_ms(str(int(t["timestamp"]) * 1000)) if str(t["timestamp"]).isdigit() else str(t["timestamp"])
    return str(t.get("date", ""))


def _normalize_event(ev: dict[str, Any]) -> dict[str, Any]:
    organizer = ev.get("organizer_calendar_id", "") or ev.get("event_organizer", {}).get("display_name", "")
    attendee_ability = ev.get("attendee_ability", "")
    start = ev.get("start_time", {})
    return {
        "event_id": ev.get("event_id", ""),
        "summary": ev.get("summary", ""),
        "description": ev.get("description", ""),
        "start": _event_time_str(ev.get("start_time", {})),
        "end": _event_time_str(ev.get("end_time", {})),
        "status": ev.get("status", ""),
        "is_all_day": isinstance(start, dict) and "date" in start and "timestamp" not in start,
        "organizer": organizer,
        "attendee_ability": attendee_ability,
    }


async def list_events_impl(
    start: str, end: str, calendar_id: str = "", timezone: str = "Asia/Shanghai", max_events: int = 50
) -> dict[str, Any]:
    """List events on a calendar between start and end. Blank calendar_id uses the bot's primary calendar."""
    start_ts = _ts_of(start, timezone)
    end_ts = _ts_of(end, timezone)
    if start_ts is None or end_ts is None:
        return _core._error("start/end must be 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'.")
    cal_id = calendar_id.strip() or await _core._get_primary_calendar_id()
    if not cal_id:
        return _core._error(
            "Could not resolve a calendar_id. Pass one, or ensure the bot's primary calendar is available."
        )
    events: list[dict[str, Any]] = []
    page_token = ""
    while len(events) < max_events:
        page_size = min(1000, max(50, max_events - len(events)))
        res = await _core._invoke(_build_list_events_request(cal_id, start_ts, end_ts, page_size, page_token))
        if not res["ok"]:
            return res
        data = res["data"] if isinstance(res["data"], dict) else {}
        for ev in data.get("items", []) if isinstance(data.get("items"), list) else []:
            if isinstance(ev, dict):
                events.append(_normalize_event(ev))
            if len(events) >= max_events:
                break
        page_token = data.get("page_token", "") if data.get("has_more") else ""
        if not page_token:
            break
    return {"ok": True, "calendar_id": cal_id, "count": len(events), "events": events}


# ── Calendar (日历) — create a separate event for each person ─────────────────
#
# For "give each person their own schedule": create one independent event per
# attendee on the bot's primary calendar, each inviting only that one person.
# Partial failures are reported per person rather than crashing the batch.


async def create_events_per_person_impl(
    summary: str,
    start: str,
    end: str,
    attendees: str,
    description: str = "",
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Create one independent event per open_id, each inviting only that person."""
    open_ids = [a.strip() for a in attendees.split(",") if a.strip()]
    if not open_ids:
        return _core._error("attendees must contain at least one comma-separated open_id.")
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for oid in open_ids:
        res = await _core.create_event_impl(summary, start, end, description, oid, timezone)
        if res.get("ok") and not res.get("attendee_warning"):
            created.append({"open_id": oid, "event_id": res.get("event_id", "")})
        else:
            failed.append({"open_id": oid, "error": res.get("attendee_warning") or res.get("message", "failed")})
    return {"ok": not failed, "count": len(created), "created": created, "failed": failed}
