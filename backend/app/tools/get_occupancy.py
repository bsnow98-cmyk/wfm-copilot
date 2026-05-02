"""
get_occupancy tool — Wave 2.

Answers "what's our occupancy by interval?" — the standard contact-center
utilization metric. Occupancy = (handled * AHT) / (scheduled_seconds),
where scheduled_seconds = scheduled_agents * interval_minutes * 60.

Returns chart.line so the curve over the day is visible at a glance.
Healthy occupancy is generally 80-90% — too high burns agents, too low
wastes payroll. The model can flag intervals outside that band in its
summary.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_occupancy",
    "description": (
        "Show agent occupancy (productive seconds / available seconds) by "
        "interval for a queue and date. Returns a single-series line chart "
        "of occupancy %. Use when the user asks 'what's our occupancy', 'are "
        "agents over/under-utilized', or 'how busy is the floor'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queue": {
                "type": "string",
                "description": "Queue name (e.g. 'sales_inbound').",
            },
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Defaults to today.",
            },
        },
        "required": ["queue"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    queue: str = args["queue"]
    target_date = _parse_date(args.get("date"))

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    # Find an active schedule covering this date, for scheduled headcount.
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
            "message": f"No schedule covers {target_date.isoformat()}.",
            "code": "NO_SCHEDULE",
        }

    rows = db.execute(
        text(
            """
            SELECT ih.interval_start,
                   ih.handled,
                   ih.aht_seconds,
                   ih.interval_minutes,
                   sc.scheduled_agents
            FROM interval_history ih
            LEFT JOIN schedule_coverage sc
              ON sc.schedule_id = :sid
             AND sc.interval_start = ih.interval_start
            WHERE ih.queue = :queue
              AND ih.interval_start >= :start
              AND ih.interval_start < :end
            ORDER BY ih.interval_start
            """
        ),
        {
            "queue": queue,
            "sid": schedule_id,
            "start": day_start,
            "end": day_end,
        },
    ).all()

    if not rows:
        return {
            "render": "error",
            "message": (
                f"No history for {queue} on {target_date.isoformat()}."
            ),
            "code": "NO_ACTUALS",
        }

    points: list[dict[str, Any]] = []
    for r in rows:
        ts: datetime = r[0]
        handled = float(r[1] or 0)
        aht = float(r[2] or 0)
        minutes = int(r[3] or 30)
        scheduled = int(r[4] or 0)
        if scheduled <= 0:
            # Skip intervals with no scheduled headcount — occupancy is /0.
            continue
        productive_sec = handled * aht
        available_sec = scheduled * minutes * 60
        occ_pct = round(productive_sec / available_sec * 100, 1)
        points.append({"x": ts.strftime("%H:%M"), "y": occ_pct})

    if not points:
        return {
            "render": "error",
            "message": (
                f"No intervals on {target_date.isoformat()} have both "
                "scheduled headcount and actuals — can't compute occupancy."
            ),
            "code": "NO_OVERLAP",
        }

    return {
        "render": "chart.line",
        "title": (
            f"Occupancy — {queue}, {target_date.isoformat()} "
            "(healthy band 80–90%)"
        ),
        "yLabel": "occupancy %",
        "series": [{"name": "Occupancy", "points": points}],
    }


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
