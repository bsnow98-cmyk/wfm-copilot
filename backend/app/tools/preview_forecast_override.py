"""
preview_forecast_override — read-only preview of pinning a forecast interval.

Surface #4 of EXECUTION_ROADMAP.md. Resolves the latest completed forecast run
for a queue, looks up the target interval's current offered volume, and mints an
apply_token pinning {run, interval, new value, version}. Applying happens in
app/routers/forecast_overrides.py via the shared apply envelope; the LLM previews
but never writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "preview_forecast_override",
    "description": (
        "Preview (read-only) overriding the forecast offered-volume for one "
        "interval of a queue's latest forecast, pinning it to an analyst value. "
        "Shows current vs proposed and surfaces an Apply button. Use when the "
        "user says e.g. 'override the 14:00 forecast for all to 320', 'pin "
        "tomorrow 09:30 to 150 calls'. Does NOT modify the forecast."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queue": {"type": "string", "description": "Queue name, e.g. 'all'."},
            "date": {"type": "string", "description": "ISO date YYYY-MM-DD of the interval."},
            "time": {"type": "string", "description": "Interval start time HH:MM (24h, UTC)."},
            "value": {"type": "number", "description": "New offered-volume value to pin."},
        },
        "required": ["queue", "date", "time", "value"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    from app.services.apply_tokens import issue_forecast_token
    from app.services.forecast_override import compute_value_version, load_interval_value

    queue = args["queue"]
    new_value = float(args["value"])
    if new_value < 0:
        return {"render": "error", "message": "value must be ≥ 0.", "code": "BAD_ARGS"}

    try:
        interval_start = datetime.fromisoformat(f"{args['date']}T{args['time']}").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return {"render": "error", "message": "Invalid date/time.", "code": "BAD_ARGS"}

    run_id = db.execute(
        text(
            """
            SELECT id FROM forecast_runs
            WHERE queue = :queue AND status = 'completed'
            ORDER BY created_at DESC LIMIT 1
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

    iso = interval_start.isoformat()
    current = load_interval_value(db, int(run_id), iso)
    if current is None:
        return {
            "render": "error",
            "message": (
                f"No forecast interval at {args['date']} {args['time']} for queue "
                f"{queue!r} (run {run_id})."
            ),
            "code": "NO_INTERVAL",
        }

    version = compute_value_version(current)
    token = issue_forecast_token(
        db,
        forecast_run_id=int(run_id),
        interval_start=iso,
        new_value=new_value,
        expected_version=version,
    )
    db.commit()

    delta = new_value - current
    return {
        "render": "table",
        "title": (
            f"Override forecast — {queue}, {args['date']} {args['time']} — "
            f"recompute staffing separately"
        ),
        "columns": ["Interval", "Current offered", "Proposed", "Δ"],
        "rows": [[f"{args['date']} {args['time']}", f"{current:.0f}", f"{new_value:.0f}", f"{delta:+.0f}"]],
        "apply_token": token.token,
        "forecast_override": {
            "queue": queue,
            "interval_label": f"{args['date']} {args['time']}",
            "current": current,
            "proposed": new_value,
        },
    }
