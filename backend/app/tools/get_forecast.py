"""
get_forecast tool — returns chart.line with forecast vs actual overlay.

Picks the most recent completed forecast_run for (queue, channel='voice'),
then joins forecast_intervals (forecast series) against interval_history
(actual series) for the date the user asked about.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_forecast",
    "description": (
        "Get the forecast vs actual call volume for a single queue and date. "
        "Returns a multi-series line chart with two series ('Forecast', 'Actual'). "
        "Use when the user asks about expected or actual volume."
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

    run_id = db.execute(
        text(
            """
            SELECT id FROM forecast_runs
            WHERE queue = :queue AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"queue": queue},
    ).scalar_one_or_none()

    if run_id is None:
        return {
            "render": "error",
            "message": f"No completed forecast run for queue {queue!r}.",
            "code": "FORECAST_NOT_FOUND",
        }

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    forecast_rows = db.execute(
        text(
            """
            SELECT interval_start, forecast_offered
            FROM forecast_intervals
            WHERE forecast_run_id = :id
              AND interval_start >= :start AND interval_start < :end
            ORDER BY interval_start
            """
        ),
        {"id": run_id, "start": day_start, "end": day_end},
    ).all()

    actual_rows = db.execute(
        text(
            """
            SELECT interval_start, offered
            FROM interval_history
            WHERE queue = :queue AND interval_start >= :start AND interval_start < :end
            ORDER BY interval_start
            """
        ),
        {"queue": queue, "start": day_start, "end": day_end},
    ).all()

    forecast_series = [
        {"x": r[0].strftime("%H:%M"), "y": float(r[1])} for r in forecast_rows
    ]
    actual_series = [
        {"x": r[0].strftime("%H:%M"), "y": float(r[1])} for r in actual_rows
    ]

    return {
        "render": "chart.line",
        "title": f"Forecast vs Actual — {queue}, {target_date.isoformat()}",
        "yLabel": "calls",
        "series": [
            {"name": "Forecast", "points": forecast_series},
            {"name": "Actual", "points": actual_series},
        ],
    }


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
