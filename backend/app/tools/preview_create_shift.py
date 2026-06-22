"""
preview_create_shift — Surface #6: add an agent to a day (create a shift).

The highest-blast-radius write, gated behind RBAC: the schedule apply endpoint it
rides (POST /schedules/apply) now requires wfm_manager+. Like the break-move
surface, this is a preview-tool wrapper — it builds the new shift's segments
(work / lunch / work) for an agent on a date and delegates to
preview_schedule_change, which mints the schedule apply token and renders the
gantt. The LLM previews; only a manager can apply.
"""
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "preview_create_shift",
    "description": (
        "Preview (read-only) creating a new shift for an agent on a day — i.e. "
        "adding them to the roster for that date. Builds a standard work/lunch/"
        "work shift and shows the resulting schedule with an Apply button "
        "(applying requires a manager). Use when the user says e.g. 'add EMP012 "
        "to Tuesday 9–5', 'put Adams on a shift Friday'. Does NOT modify the "
        "schedule."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "employee_id": {"type": "string", "description": "External employee_id to schedule."},
            "date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
            "start_time": {"type": "string", "description": "Shift start HH:MM (UTC). Default 09:00."},
            "end_time": {"type": "string", "description": "Shift end HH:MM (UTC). Default 17:00."},
            "lunch_start": {"type": "string", "description": "Lunch start HH:MM (UTC). Default 12:00."},
        },
        "required": ["employee_id", "date"],
    },
}


def _at(d: date_cls, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).replace(
        hour=int(h), minute=int(m)
    )


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    from app.tools.preview_schedule_change import handler as preview_schedule

    employee_id = args["employee_id"]
    try:
        target = date_cls.fromisoformat(args["date"])
        start = _at(target, args.get("start_time") or "09:00")
        end = _at(target, args.get("end_time") or "17:00")
        lunch_start = _at(target, args.get("lunch_start") or "12:00")
    except (ValueError, KeyError):
        return {"render": "error", "message": "Invalid date/time.", "code": "BAD_ARGS"}

    lunch_end = lunch_start + timedelta(minutes=30)
    if not (start < lunch_start < lunch_end < end):
        return {
            "render": "error",
            "message": "Times must satisfy start < lunch_start < lunch_end < end.",
            "code": "BAD_WINDOW",
        }

    agent = (
        db.execute(
            text("SELECT full_name FROM agents WHERE employee_id = :eid AND active = TRUE"),
            {"eid": employee_id},
        )
        .mappings()
        .one_or_none()
    )
    if agent is None:
        return {"render": "error", "message": f"Active agent {employee_id} not found.", "code": "NO_AGENT"}

    # Standard shift: work, lunch, work. preview_schedule_change inserts these
    # (the agent has no segments that day) and mints the schedule apply token.
    changes = [
        {"agent_id": employee_id, "start": start.isoformat(), "end": lunch_start.isoformat(), "activity": "available"},
        {"agent_id": employee_id, "start": lunch_start.isoformat(), "end": lunch_end.isoformat(), "activity": "lunch"},
        {"agent_id": employee_id, "start": lunch_end.isoformat(), "end": end.isoformat(), "activity": "available"},
    ]
    return preview_schedule({"date": target.isoformat(), "changes": changes}, db)
