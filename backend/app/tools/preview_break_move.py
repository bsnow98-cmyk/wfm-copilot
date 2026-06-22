"""
preview_break_move — read-only preview of moving one agent's planned break.

Surface #3 of EXECUTION_ROADMAP.md. The roadmap calls this "mostly a
preview-tool wrapper" because the *write* path already exists: moving a break
is just a schedule-segment edit. This tool resolves the agent's next break,
builds the two-segment change set (free the old slot back to work, place the
break at the new time), and delegates to preview_schedule_change — which mints
the schedule apply_token and renders the gantt. Apply rides the shipped
POST /schedules/apply path; no new endpoint, table, or renderer.
"""
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "preview_break_move",
    "description": (
        "Preview (read-only) moving one agent's upcoming planned break earlier "
        "or later. Resolves the agent's next break and shows the resulting "
        "schedule with an Apply button. Use after recommend_break_shift when "
        "the user says e.g. 'move EMP012's break 30 min earlier'. Does NOT "
        "modify the schedule."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "employee_id": {
                "type": "string",
                "description": "External employee_id whose break to move.",
            },
            "direction": {
                "type": "string",
                "enum": ["earlier", "later"],
            },
            "minutes": {
                "type": "integer",
                "minimum": 15,
                "maximum": 120,
                "description": "How many minutes to shift the break. Default 30.",
            },
            "date": {
                "type": "string",
                "description": (
                    "ISO date YYYY-MM-DD to find the break on. Defaults to the "
                    "agent's next break from the current sim time."
                ),
            },
        },
        "required": ["employee_id", "direction"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    from app.services.realtime_clock import sim_now
    from app.tools.preview_schedule_change import handler as preview_schedule

    employee_id = args["employee_id"]
    direction = args.get("direction") or "earlier"
    if direction not in ("earlier", "later"):
        return {"render": "error", "message": "direction must be 'earlier' or 'later'.", "code": "BAD_ARGS"}
    minutes = int(args.get("minutes") or 30)

    agent = (
        db.execute(
            text("SELECT id, full_name FROM agents WHERE employee_id = :eid AND active = TRUE"),
            {"eid": employee_id},
        )
        .mappings()
        .one_or_none()
    )
    if agent is None:
        return {"render": "error", "message": f"Active agent {employee_id} not found.", "code": "NO_AGENT"}

    # Locate the break to move: on a given date, or the next one from sim_now.
    if args.get("date"):
        target_date = date_cls.fromisoformat(args["date"])
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        brk = (
            db.execute(
                text(
                    """
                    SELECT start_time, end_time FROM shift_segments
                    WHERE agent_id = :aid AND segment_type = 'break'
                      AND start_time >= :ds AND start_time < :de
                    ORDER BY start_time LIMIT 1
                    """
                ),
                {"aid": agent["id"], "ds": day_start, "de": day_end},
            )
            .mappings()
            .one_or_none()
        )
    else:
        now = sim_now(db)
        brk = (
            db.execute(
                text(
                    """
                    SELECT start_time, end_time FROM shift_segments
                    WHERE agent_id = :aid AND segment_type = 'break' AND start_time > :now
                    ORDER BY start_time LIMIT 1
                    """
                ),
                {"aid": agent["id"], "now": now},
            )
            .mappings()
            .one_or_none()
        )

    if brk is None:
        when = f"on {args['date']}" if args.get("date") else "upcoming"
        return {
            "render": "error",
            "message": f"No {when} break found for {agent['full_name']} ({employee_id}).",
            "code": "NO_BREAK",
        }

    old_start: datetime = brk["start_time"]
    old_end: datetime = brk["end_time"]
    delta = timedelta(minutes=minutes if direction == "later" else -minutes)
    new_start = old_start + delta
    new_end = old_end + delta

    # Two edits: free the old break window back to work, place the break at the
    # new time. preview_schedule_change replaces overlapping segments per change,
    # so this reads as a clean move. Order matters only for readability.
    changes = [
        {
            "agent_id": employee_id,
            "start": old_start.isoformat(),
            "end": old_end.isoformat(),
            "activity": "available",
        },
        {
            "agent_id": employee_id,
            "start": new_start.isoformat(),
            "end": new_end.isoformat(),
            "activity": "break",
        },
    ]
    return preview_schedule(
        {"date": old_start.date().isoformat(), "changes": changes}, db
    )
