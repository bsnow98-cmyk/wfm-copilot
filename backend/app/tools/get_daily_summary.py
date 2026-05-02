"""
get_daily_summary tool — Wave 1.

Answers "give me the daily/weekly summary" — the leadership digest. One
table of KPIs (value vs target) for a single queue and date. Pulls from
interval_history (actuals), forecast_intervals (predicted), and
schedule_coverage (planned). Anomaly count is best-effort; missing
table is treated as zero.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_daily_summary",
    "description": (
        "Daily KPI digest for a queue and date — total volume, SL avg, ASA "
        "avg, abandon rate, forecast accuracy, schedule fit, anomaly count. "
        "Returns a metric/value/target table. Use when the user asks for "
        "a 'daily summary', 'recap', 'how did we do', or 'EOD digest'."
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
            "sl_target": {
                "type": "number",
                "description": "SL target as a fraction (0.8 = 80%). Defaults to 0.8.",
            },
            "asa_target": {
                "type": "number",
                "description": "ASA target in seconds. Defaults to 20.",
            },
        },
        "required": ["queue"],
    },
}

_COLUMNS = ["metric", "value", "target"]


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    queue: str = args["queue"]
    target_date = _parse_date(args.get("date"))
    sl_target: float = float(args.get("sl_target", 0.8))
    asa_target: float = float(args.get("asa_target", 20))

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    actuals = (
        db.execute(
            text(
                """
                SELECT
                    SUM(offered) AS offered,
                    SUM(handled) AS handled,
                    SUM(abandoned) AS abandoned,
                    AVG(service_level) AS sl_avg,
                    AVG(asa_seconds) AS asa_avg,
                    AVG(aht_seconds) AS aht_avg,
                    COUNT(*) FILTER (WHERE service_level < :sl) AS sl_misses
                FROM interval_history
                WHERE queue = :queue
                  AND interval_start >= :start AND interval_start < :end
                """
            ),
            {
                "queue": queue,
                "start": day_start,
                "end": day_end,
                "sl": sl_target,
            },
        )
        .mappings()
        .one()
    )

    if not actuals["offered"]:
        return {
            "render": "error",
            "message": (
                f"No history for {queue} on {target_date.isoformat()}. "
                "Nothing to summarize."
            ),
            "code": "NO_ACTUALS",
        }

    # Forecast totals (most recent completed run for this queue).
    forecast_total = _forecast_total_offered(db, queue, day_start, day_end)

    # Schedule fit (active schedule, planned vs scheduled FTE-intervals).
    sched_required, sched_scheduled = _schedule_totals(db, target_date, day_start, day_end)

    # Anomaly count for this queue/day. Best-effort.
    anomaly_count = _anomaly_count(db, queue, target_date)

    offered = float(actuals["offered"] or 0)
    handled = float(actuals["handled"] or 0)
    abandoned = float(actuals["abandoned"] or 0)
    sl_avg = float(actuals["sl_avg"] or 0)
    asa_avg = float(actuals["asa_avg"] or 0)
    aht_avg = float(actuals["aht_avg"] or 0)
    sl_misses = int(actuals["sl_misses"] or 0)

    abandon_rate = (abandoned / offered * 100) if offered > 0 else 0.0

    fcst_accuracy_str = "—"
    if forecast_total and offered > 0:
        bias_pct = (forecast_total - offered) / offered * 100
        fcst_accuracy_str = f"{forecast_total:.0f} fcst / {offered:.0f} act ({bias_pct:+.1f}%)"

    schedule_fit_str = "—"
    if sched_required:
        schedule_fit_str = (
            f"{sched_scheduled} sched / {sched_required} reqd"
            f" ({(sched_scheduled / sched_required * 100):.0f}%)"
        )

    rows: list[list[Any]] = [
        ["Volume offered", int(offered), "—"],
        ["Volume handled", int(handled), "—"],
        [
            "Service level (avg)",
            f"{sl_avg * 100:.1f}%",
            f"{sl_target * 100:.0f}%",
        ],
        [
            "ASA (avg)",
            f"{asa_avg:.1f}s",
            f"{asa_target:.0f}s",
        ],
        ["AHT (avg)", f"{aht_avg:.0f}s", "—"],
        ["Abandon rate", f"{abandon_rate:.2f}%", "<3.00%"],
        ["SL miss intervals", sl_misses, "0"],
        ["Forecast vs actual", fcst_accuracy_str, "±5%"],
        ["Schedule fit", schedule_fit_str, "100%"],
        ["Anomalies detected", anomaly_count, "0"],
    ]

    return {
        "render": "table",
        "title": f"Daily summary — {queue}, {target_date.isoformat()}",
        "columns": _COLUMNS,
        "rows": rows,
    }


def _forecast_total_offered(
    db: Session, queue: str, day_start: datetime, day_end: datetime
) -> float | None:
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
        return None
    val = db.execute(
        text(
            """
            SELECT SUM(forecast_offered) FROM forecast_intervals
            WHERE forecast_run_id = :rid
              AND interval_start >= :start AND interval_start < :end
            """
        ),
        {"rid": run_id, "start": day_start, "end": day_end},
    ).scalar_one()
    return float(val) if val is not None else None


def _schedule_totals(
    db: Session, target_date: date, day_start: datetime, day_end: datetime
) -> tuple[int, int]:
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
        return 0, 0
    row = db.execute(
        text(
            """
            SELECT
                COALESCE(SUM(required_agents), 0)  AS reqd,
                COALESCE(SUM(scheduled_agents), 0) AS sched
            FROM schedule_coverage
            WHERE schedule_id = :sid
              AND interval_start >= :start AND interval_start < :end
            """
        ),
        {"sid": schedule_id, "start": day_start, "end": day_end},
    ).one()
    return int(row[0]), int(row[1])


def _anomaly_count(db: Session, queue: str, target_date: date) -> int:
    try:
        return int(
            db.execute(
                text(
                    """
                    SELECT COUNT(*) FROM anomalies
                    WHERE queue = :q AND date = :d
                    """
                ),
                {"q": queue, "d": target_date},
            ).scalar_one()
        )
    except ProgrammingError:
        db.rollback()
        return 0


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)
