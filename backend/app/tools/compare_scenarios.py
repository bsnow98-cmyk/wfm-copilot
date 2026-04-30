"""
compare_scenarios tool — runs Erlang C across multiple SL/ASA combinations
against the same forecast and returns side-by-side staffing requirements.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "compare_scenarios",
    "description": (
        "Compare staffing under multiple SL/ASA scenarios for the same queue's "
        "latest forecast. Each scenario is a {name, sl, asa, shrinkage?} object. "
        "Returns side-by-side required-by-interval columns. Use when the user "
        "asks 'what if we tightened to 90/15?' or 'compare current vs target'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queue": {"type": "string"},
            "scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "sl": {"type": "number"},
                        "asa": {"type": "number"},
                        "shrinkage": {"type": "number"},
                    },
                    "required": ["name", "sl", "asa"],
                },
                "minItems": 2,
                "maxItems": 4,
            },
        },
        "required": ["queue", "scenarios"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    queue: str = args["queue"]
    scenarios: list[dict[str, Any]] = args["scenarios"]

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
                SELECT forecast_offered, forecast_aht_seconds
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

    out: list[dict[str, Any]] = []
    for sc in scenarios:
        sl = float(sc["sl"])
        asa = float(sc["asa"])
        shrinkage = float(sc.get("shrinkage", 0.30))
        required_by_interval: list[int] = []
        for iv in intervals:
            r = required_agents(
                forecast_offered=float(iv["forecast_offered"] or 0),
                aht_seconds=float(iv["forecast_aht_seconds"] or 0),
                sl_target=sl,
                target_asa_sec=asa,
                shrinkage=shrinkage,
            )
            required_by_interval.append(int(r["required_agents"]))
        out.append(
            {
                "name": sc["name"],
                "required_by_interval": required_by_interval,
                "sla": sl,
                "asa_seconds": asa,
            }
        )

    return {"render": "scenarios", "scenarios": out}
