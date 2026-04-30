"""
get_schedule tool — returns the agent-by-interval Gantt for a date.

Joins shift_segments + agents + schedules, filters to one date, maps each
segment_type to the closed Gantt activity enum.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# segment_type stored in DB → activity enum the frontend Gantt knows about.
# Anything unmapped gets 'shrinkage' (visible but distinct from 'available').
_ACTIVITY_MAP = {
    "work": "available",
    "break": "break",
    "lunch": "lunch",
    "training": "training",
    "meeting": "meeting",
    "off": "off",
}

definition: dict[str, Any] = {
    "name": "get_schedule",
    "description": (
        "Get the published schedule for a date as an agent-by-interval Gantt. "
        "Each agent shows their segments (available, break, lunch, training, etc). "
        "Use when the user asks who is working when."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Defaults to today.",
            },
            "queue": {
                "type": "string",
                "description": "Optional queue filter. Currently advisory; the schema doesn't yet partition by queue.",
            },
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    target = _parse_date(args.get("date"))
    day_start = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    rows = (
        db.execute(
            text(
                """
                SELECT a.employee_id, a.full_name,
                       s.segment_type, s.start_time, s.end_time
                FROM shift_segments s
                JOIN agents a ON a.id = s.agent_id
                WHERE s.start_time < :end AND s.end_time > :start
                ORDER BY a.full_name, s.start_time
                """
            ),
            {"start": day_start, "end": day_end},
        )
        .mappings()
        .all()
    )

    by_agent: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["employee_id"]
        if key not in by_agent:
            by_agent[key] = {"id": key, "name": r["full_name"], "segments": []}
        by_agent[key]["segments"].append(
            {
                "start": r["start_time"].isoformat(),
                "end": r["end_time"].isoformat(),
                "activity": _ACTIVITY_MAP.get(r["segment_type"], "shrinkage"),
            }
        )

    return {
        "render": "gantt",
        "date": target.isoformat(),
        "agents": list(by_agent.values()),
    }


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
