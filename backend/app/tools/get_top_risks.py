"""
get_top_risks tool — Wave 1.

Answers "what are the top risks for tomorrow?" — the morning-stand-up
question. Aggregates risks from three signals and ranks them:

    1. Schedule shortfalls   — schedule_coverage.shortage > 0
    2. Detected anomalies    — anomalies on or near the target date
    3. Forecast peaks        — intervals > 1.5x daily mean (load concentration)

Each signal contributes a normalized severity score; the top N are
returned as a single ranked table.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_top_risks",
    "description": (
        "Rank the top staffing/operations risks for a given date — schedule "
        "shortfalls, detected anomalies, and forecast peaks combined into a "
        "single sorted table. Use when the user asks 'what should I worry "
        "about tomorrow', 'top risks for today', or 'morning brief'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD. Defaults to tomorrow.",
            },
            "queue": {
                "type": "string",
                "description": "Optional queue filter.",
            },
            "limit": {
                "type": "integer",
                "description": "Max risks to return (default 10).",
            },
        },
    },
}

_COLUMNS = ["rank", "when", "type", "severity", "description"]

_SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    target_date = _parse_date(args.get("date"))
    queue: str | None = args.get("queue")
    limit: int = int(args.get("limit", 10))

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    risks: list[dict[str, Any]] = []
    risks.extend(_shortfall_risks(db, target_date, day_start, day_end))
    risks.extend(_anomaly_risks(db, target_date, queue))
    risks.extend(_forecast_peak_risks(db, queue, day_start, day_end))

    # Sort: severity desc, score desc.
    risks.sort(
        key=lambda r: (_SEVERITY_ORDER[r["severity"]], r["score"]),
        reverse=True,
    )
    risks = risks[:limit]

    rows: list[list[Any]] = [
        [i + 1, r["when"], r["type"], r["severity"], r["description"]]
        for i, r in enumerate(risks)
    ]

    return {
        "render": "table",
        "title": f"Top risks — {target_date.isoformat()}"
        + (f" ({queue})" if queue else ""),
        "columns": _COLUMNS,
        "rows": rows,
    }


def _shortfall_risks(
    db: Session, target_date: date, day_start: datetime, day_end: datetime
) -> list[dict[str, Any]]:
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
        return []

    rows = db.execute(
        text(
            """
            SELECT interval_start, required_agents, scheduled_agents, shortage
            FROM schedule_coverage
            WHERE schedule_id = :sid
              AND interval_start >= :start AND interval_start < :end
              AND shortage > 0
            ORDER BY shortage DESC
            LIMIT 5
            """
        ),
        {"sid": schedule_id, "start": day_start, "end": day_end},
    ).all()

    out: list[dict[str, Any]] = []
    for r in rows:
        ts: datetime = r[0]
        short = int(r[3])
        required = int(r[1] or 0)
        sev = (
            "high"
            if required and short / required >= 0.2
            else "medium"
            if short >= 2
            else "low"
        )
        out.append(
            {
                "when": ts.strftime("%H:%M"),
                "type": "shortfall",
                "severity": sev,
                "score": short,
                "description": (
                    f"{short} short ({r[2]} scheduled / {required} required)"
                ),
            }
        )
    return out


def _anomaly_risks(
    db: Session, target_date: date, queue: str | None
) -> list[dict[str, Any]]:
    where = "WHERE date = :d"
    params: dict[str, Any] = {"d": target_date}
    if queue:
        where += " AND queue = :queue"
        params["queue"] = queue

    sql = f"""
        SELECT id, interval_start, queue, category, severity, score, note
        FROM anomalies
        {where}
        ORDER BY severity DESC, score DESC
        LIMIT 5
    """
    try:
        rows = db.execute(text(sql), params).all()
    except ProgrammingError:
        # Anomalies table may not exist in this env. Treat as no signal.
        db.rollback()
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        ts: datetime = r[1]
        out.append(
            {
                "when": ts.strftime("%H:%M"),
                "type": f"anomaly:{r[3]}",
                "severity": r[4],
                "score": float(r[5] or 0),
                "description": r[6] or f"{r[3]} on {r[2]}",
            }
        )
    return out


def _forecast_peak_risks(
    db: Session,
    queue: str | None,
    day_start: datetime,
    day_end: datetime,
) -> list[dict[str, Any]]:
    if queue is None:
        return []
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
        return []

    rows = db.execute(
        text(
            """
            SELECT interval_start, forecast_offered
            FROM forecast_intervals
            WHERE forecast_run_id = :rid
              AND interval_start >= :start AND interval_start < :end
            """
        ),
        {"rid": run_id, "start": day_start, "end": day_end},
    ).all()
    if not rows:
        return []

    values = [float(r[1] or 0) for r in rows]
    if not values:
        return []
    mean = sum(values) / len(values)
    if mean <= 0:
        return []

    out: list[dict[str, Any]] = []
    for ts, fcst in zip([r[0] for r in rows], values):
        if fcst >= mean * 1.5:
            ratio = fcst / mean
            sev = "high" if ratio >= 2.0 else "medium"
            out.append(
                {
                    "when": ts.strftime("%H:%M"),
                    "type": "forecast_peak",
                    "severity": sev,
                    "score": ratio,
                    "description": (
                        f"forecast {fcst:.0f} vs day-mean {mean:.0f} "
                        f"({ratio:.1f}x)"
                    ),
                }
            )
    # Keep at most the 3 hottest peaks so they don't crowd out other signals.
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:3]


def _parse_date(value: str | None) -> date:
    if value is None:
        return (datetime.now(timezone.utc) + timedelta(days=1)).date()
    return date.fromisoformat(value)
