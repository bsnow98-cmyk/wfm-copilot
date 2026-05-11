"""
get_training_calendar — upcoming training/coaching/meeting events.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_training_calendar",
    "description": (
        "Upcoming training calendar — team meetings, coaching slots, "
        "new-hire classes, skill cert events. Use when the user asks "
        "'what training is on the books', 'training calendar', 'upcoming "
        "coaching sessions'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "horizon_days": {"type": "integer", "minimum": 1, "maximum": 90},
            "event_type": {
                "type": "string",
                "enum": [
                    "team_meeting",
                    "coaching",
                    "skill_cert",
                    "new_hire_class",
                    "system_training",
                ],
            },
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    horizon_days = int(args.get("horizon_days") or 14)
    event_type = args.get("event_type")

    params: dict[str, Any] = {"horizon_days": horizon_days}
    extra = ""
    if event_type:
        extra = "AND event_type = :etype"
        params["etype"] = event_type

    rows = (
        db.execute(
            text(
                f"""
                SELECT te.start_ts, te.end_ts, te.event_type, te.title,
                       te.required, te.target_skill_id, s.name AS skill_name,
                       (SELECT COUNT(*) FROM training_attendees ta WHERE ta.training_event_id = te.id) AS attendee_count
                FROM training_events te
                LEFT JOIN skills s ON s.id = te.target_skill_id
                WHERE te.start_ts >= sim_now()
                  AND te.start_ts < sim_now() + (:horizon_days || ' days')::interval
                  {extra}
                ORDER BY te.start_ts
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    table_rows = [
        [
            r["start_ts"].strftime("%Y-%m-%d %H:%M"),
            f"{int((r['end_ts'] - r['start_ts']).total_seconds() // 60)}m",
            r["event_type"],
            r["title"],
            r["skill_name"] or "—",
            int(r["attendee_count"]),
            "✓" if r["required"] else "",
        ]
        for r in rows
    ]
    return {
        "render": "table",
        "title": f"Training calendar — next {horizon_days}d — {len(rows)} events",
        "columns": ["When", "Duration", "Type", "Title", "Skill", "Attendees", "Required"],
        "rows": table_rows,
    }
