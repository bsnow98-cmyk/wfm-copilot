"""
get_intraday_gaps tool — Wave 1.

Answers "where am I short today, interval-by-interval?" — the most common
mid-shift ops question. Returns a chart.line with two series: Required and
Scheduled, derived from schedule_coverage for the active schedule covering
the target date. Visual gap = ops attention.

Read-only. Picks a published schedule first, then falls back to the most
recent schedule whose date range covers the target date.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_intraday_gaps",
    "description": (
        "Show interval-by-interval staffing gaps for a single date — required "
        "vs scheduled headcount by half-hour. Returns a multi-series line chart "
        "('Required', 'Scheduled'). Use when the user asks 'where am I short "
        "today', 'when do I have gaps', or 'show me coverage by interval'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Defaults to today.",
            },
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    target_date = _parse_date(args.get("date"))

    schedule_id = db.execute(
        text(
            """
            SELECT id FROM schedules
            WHERE start_date <= :d AND end_date >= :d
            ORDER BY (status = 'published') DESC, created_at DESC
            LIMIT 1
            """
        ),
        {"d": target_date},
    ).scalar_one_or_none()

    if schedule_id is None:
        return {
            "render": "error",
            "message": (
                f"No schedule covers {target_date.isoformat()}. "
                "Generate or import a schedule first."
            ),
            "code": "NO_SCHEDULE",
        }

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    rows = db.execute(
        text(
            """
            SELECT interval_start, required_agents, scheduled_agents
            FROM schedule_coverage
            WHERE schedule_id = :id
              AND interval_start >= :start AND interval_start < :end
            ORDER BY interval_start
            """
        ),
        {"id": schedule_id, "start": day_start, "end": day_end},
    ).all()

    if not rows:
        return {
            "render": "error",
            "message": (
                f"Schedule {schedule_id} has no coverage rows for "
                f"{target_date.isoformat()}. Recompute coverage."
            ),
            "code": "NO_COVERAGE",
        }

    required_pts = [
        {"x": r[0].strftime("%H:%M"), "y": int(r[1] or 0)} for r in rows
    ]
    scheduled_pts = [
        {"x": r[0].strftime("%H:%M"), "y": int(r[2] or 0)} for r in rows
    ]

    return {
        "render": "chart.line",
        "title": f"Intraday gaps — {target_date.isoformat()}",
        "yLabel": "agents",
        "series": [
            {"name": "Required", "points": required_pts},
            {"name": "Scheduled", "points": scheduled_pts},
        ],
    }


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
