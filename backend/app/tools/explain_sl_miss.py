"""
explain_sl_miss tool — Wave 1.

Answers "why did we miss SL [yesterday/on date]?" — the post-mortem ask.
Returns a table of intervals where SL fell below the target, each with a
diagnosed cause: volume spike, AHT spike, understaffed, or high abandons.

Causes are computed by comparing actuals against the most recent
completed forecast and the active schedule's coverage. Diagnoses are
heuristic — surfaced for humans to confirm, not autopilot rules.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "explain_sl_miss",
    "description": (
        "Diagnose service-level misses for a queue and date. Returns a table "
        "of intervals where SL fell below target, each tagged with a probable "
        "cause (volume spike, AHT spike, understaffed, abandons). Use when "
        "the user asks 'why did we miss SL', 'what happened yesterday', or "
        "'why was the day rough'."
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
                "description": "ISO date YYYY-MM-DD. Defaults to yesterday.",
            },
            "sl_target": {
                "type": "number",
                "description": "SL target as a fraction (0.8 = 80%). Defaults to 0.8.",
            },
        },
        "required": ["queue"],
    },
}

_COLUMNS = [
    "interval",
    "sl",
    "offered",
    "fcst",
    "sched",
    "reqd",
    "cause",
]

_VOLUME_SPIKE = 1.15  # actual > forecast * 1.15
_AHT_SPIKE = 1.15
_ABANDON_RATE = 0.05


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    queue: str = args["queue"]
    target_date = _parse_date(args.get("date"))
    sl_target: float = float(args.get("sl_target", 0.8))

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    forecast_run_id = db.execute(
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

    # Pull actuals first; we need at minimum interval_history to find misses.
    actuals = (
        db.execute(
            text(
                """
                SELECT interval_start, offered, handled, abandoned,
                       aht_seconds, asa_seconds, service_level
                FROM interval_history
                WHERE queue = :queue
                  AND interval_start >= :start AND interval_start < :end
                ORDER BY interval_start
                """
            ),
            {"queue": queue, "start": day_start, "end": day_end},
        )
        .mappings()
        .all()
    )

    if not actuals:
        return {
            "render": "error",
            "message": (
                f"No actuals for {queue} on {target_date.isoformat()}. "
                "Nothing to diagnose."
            ),
            "code": "NO_ACTUALS",
        }

    # Forecast lookup (interval -> {offered, aht}). Empty dict if no forecast.
    forecast_by_ts: dict[datetime, dict[str, float]] = {}
    if forecast_run_id is not None:
        for r in db.execute(
            text(
                """
                SELECT interval_start, forecast_offered, forecast_aht_seconds
                FROM forecast_intervals
                WHERE forecast_run_id = :rid
                  AND interval_start >= :start AND interval_start < :end
                """
            ),
            {"rid": forecast_run_id, "start": day_start, "end": day_end},
        ).mappings():
            forecast_by_ts[r["interval_start"]] = {
                "offered": float(r["forecast_offered"] or 0),
                "aht": float(r["forecast_aht_seconds"] or 0),
            }

    # Coverage lookup (interval -> {required, scheduled}).
    coverage_by_ts: dict[datetime, dict[str, int]] = {}
    if schedule_id is not None:
        for r in db.execute(
            text(
                """
                SELECT interval_start, required_agents, scheduled_agents
                FROM schedule_coverage
                WHERE schedule_id = :sid
                  AND interval_start >= :start AND interval_start < :end
                """
            ),
            {"sid": schedule_id, "start": day_start, "end": day_end},
        ).mappings():
            coverage_by_ts[r["interval_start"]] = {
                "required": int(r["required_agents"] or 0),
                "scheduled": int(r["scheduled_agents"] or 0),
            }

    rows: list[list[Any]] = []
    for a in actuals:
        sl = a["service_level"]
        if sl is None or float(sl) >= sl_target:
            continue

        ts: datetime = a["interval_start"]
        offered = float(a["offered"] or 0)
        abandoned = float(a["abandoned"] or 0)
        aht = float(a["aht_seconds"] or 0)
        fcst = forecast_by_ts.get(ts, {})
        cov = coverage_by_ts.get(ts, {})

        cause = _diagnose(
            offered=offered,
            forecast_offered=fcst.get("offered", 0.0),
            aht=aht,
            forecast_aht=fcst.get("aht", 0.0),
            abandoned=abandoned,
            scheduled=cov.get("scheduled", 0),
            required=cov.get("required", 0),
        )

        rows.append(
            [
                ts.strftime("%H:%M"),
                round(float(sl) * 100, 1),
                int(offered),
                int(fcst.get("offered", 0)) if fcst else "-",
                cov.get("scheduled", "-"),
                cov.get("required", "-"),
                cause,
            ]
        )

    if not rows:
        return {
            "render": "table",
            "title": (
                f"SL miss diagnosis — {queue}, {target_date.isoformat()} "
                f"(target {int(sl_target*100)}%): no misses"
            ),
            "columns": _COLUMNS,
            "rows": [],
        }

    return {
        "render": "table",
        "title": (
            f"SL miss diagnosis — {queue}, {target_date.isoformat()} "
            f"(target {int(sl_target*100)}%, {len(rows)} interval(s))"
        ),
        "columns": _COLUMNS,
        "rows": rows,
    }


def _diagnose(
    *,
    offered: float,
    forecast_offered: float,
    aht: float,
    forecast_aht: float,
    abandoned: float,
    scheduled: int,
    required: int,
) -> str:
    """Heuristic single-cause label. Order matters: pick the worst offender."""
    # Understaffed by schedule is the most actionable — name it first.
    if required and scheduled and scheduled < required:
        return f"understaffed ({required - scheduled} short)"
    if forecast_offered and offered > forecast_offered * _VOLUME_SPIKE:
        pct = (offered / forecast_offered - 1) * 100
        return f"volume spike (+{pct:.0f}%)"
    if forecast_aht and aht > forecast_aht * _AHT_SPIKE:
        pct = (aht / forecast_aht - 1) * 100
        return f"AHT spike (+{pct:.0f}%)"
    if offered and abandoned / offered > _ABANDON_RATE:
        rate = abandoned / offered * 100
        return f"high abandons ({rate:.0f}%)"
    return "unexplained"


def _parse_date(value: str | None) -> date:
    if value is None:
        return (datetime.now(timezone.utc) - timedelta(days=1)).date()
    return date.fromisoformat(value)
