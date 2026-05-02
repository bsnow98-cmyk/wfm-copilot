"""
get_forecast_accuracy tool — Wave 1.

Answers "how is the day tracking vs forecast right now?" — quantifies
forecast quality intraday. Returns a table of intervals (where actuals
exist) with forecast, actual, error, and pct error. MAPE/WAPE go in the
title so the model can quote the headline number.

Only intervals with both forecast and actual rows are included (left
join would surface gaps; ops want to see the deviation, not the missing
data).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_forecast_accuracy",
    "description": (
        "Compare forecast vs actual call volume for a queue and date — "
        "interval-by-interval table with MAPE/WAPE in the title. Use when the "
        "user asks 'how is the forecast tracking', 'are we beating the "
        "forecast', or 'what's our MAPE today'. Different from get_forecast: "
        "that one charts the curves; this one quantifies the miss."
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

_COLUMNS = ["interval", "forecast", "actual", "error", "error_pct"]


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

    rows = db.execute(
        text(
            """
            SELECT fi.interval_start, fi.forecast_offered, ih.offered
            FROM forecast_intervals fi
            JOIN interval_history ih
              ON ih.interval_start = fi.interval_start
             AND ih.queue = :queue
            WHERE fi.forecast_run_id = :rid
              AND fi.interval_start >= :start
              AND fi.interval_start < :end
            ORDER BY fi.interval_start
            """
        ),
        {"rid": run_id, "queue": queue, "start": day_start, "end": day_end},
    ).all()

    if not rows:
        return {
            "render": "error",
            "message": (
                f"No actuals yet for {queue} on {target_date.isoformat()} — "
                "the day hasn't accumulated history. Try get_forecast for the "
                "predicted curve."
            ),
            "code": "NO_ACTUALS",
        }

    table_rows: list[list[Any]] = []
    sum_abs_err = 0.0
    sum_actual = 0.0
    sum_forecast = 0.0
    sum_pct_err = 0.0
    pct_count = 0

    for r in rows:
        ts: datetime = r[0]
        fcst = float(r[1] or 0)
        actual = float(r[2] or 0)
        err = actual - fcst
        # MAPE denominator must be actual; skip rows where actual=0 to avoid /0
        # which is the textbook MAPE caveat. WAPE uses sum-of-actuals.
        pct = (abs(err) / actual * 100) if actual > 0 else None
        sum_abs_err += abs(err)
        sum_actual += actual
        sum_forecast += fcst
        if pct is not None:
            sum_pct_err += pct
            pct_count += 1
        table_rows.append(
            [
                ts.strftime("%H:%M"),
                round(fcst, 1),
                round(actual, 1),
                round(err, 1),
                round(pct, 1) if pct is not None else "-",
            ]
        )

    mape = round(sum_pct_err / pct_count, 1) if pct_count else None
    wape = round(sum_abs_err / sum_actual * 100, 1) if sum_actual > 0 else None

    bias_str = ""
    if sum_actual > 0:
        bias = (sum_forecast - sum_actual) / sum_actual * 100
        bias_str = f", bias {bias:+.1f}%"

    return {
        "render": "table",
        "title": (
            f"Forecast accuracy — {queue}, {target_date.isoformat()} "
            f"(MAPE {mape}%, WAPE {wape}%{bias_str})"
        ),
        "columns": _COLUMNS,
        "rows": table_rows,
    }


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
