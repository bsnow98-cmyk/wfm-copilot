"""
get_staffing tool — returns chart.bar of required agents by interval.

Read-style: walks the latest completed forecast for the queue, applies Erlang C
per interval, returns a chart.bar. No DB writes (the staffing router has the
write-path; this is for chat).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_staffing",
    "description": (
        "Compute required agents per 30-min interval using Erlang C, given a "
        "queue and service-level target (SL fraction, ASA seconds). Returns a "
        "bar chart of required-by-interval. Use when the user asks how many "
        "agents are needed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queue": {
                "type": "string",
                "description": "Queue name. Used to pick the latest completed forecast run.",
            },
            "sl": {
                "type": "number",
                "description": "Service level target as a fraction (0.8 = 80%).",
            },
            "asa": {
                "type": "number",
                "description": "Maximum acceptable average speed of answer in seconds.",
            },
            "shrinkage": {
                "type": "number",
                "description": "Fraction of paid time non-productive. Defaults to 0.30.",
            },
        },
        "required": ["queue", "sl", "asa"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    queue: str = args["queue"]
    sl: float = float(args["sl"])
    asa: float = float(args["asa"])
    shrinkage: float = float(args.get("shrinkage", 0.30))

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

    intervals = (
        db.execute(
            text(
                """
                SELECT interval_start, forecast_offered, forecast_aht_seconds
                FROM forecast_intervals
                WHERE forecast_run_id = :id
                ORDER BY interval_start
                """
            ),
            {"id": run_id},
        )
        .mappings()
        .all()
    )

    from app.services.staffing import required_agents

    bars: list[dict[str, Any]] = []
    for iv in intervals:
        result = required_agents(
            forecast_offered=float(iv["forecast_offered"] or 0),
            aht_seconds=float(iv["forecast_aht_seconds"] or 0),
            sl_target=sl,
            target_asa_sec=asa,
            shrinkage=shrinkage,
        )
        bars.append(
            {
                "label": iv["interval_start"].strftime("%H:%M"),
                "value": int(result["required_agents"]),
            }
        )

    return {
        "render": "chart.bar",
        "title": (
            f"Required agents — {queue} (SL {int(sl * 100)}% / ASA {int(asa)}s)"
        ),
        "bars": bars,
    }
