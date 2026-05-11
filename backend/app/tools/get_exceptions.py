"""
get_exceptions — list adherence exceptions (late_start, missed_break,
early_out, extended_break, unplanned_aux, no_show) over a window.

Reads `adherence_exceptions` directly — that table is populated alongside
aux events by the synthetic-data generator (and in production would be
populated by a nightly job that walks the planned/actual overlap).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_exceptions",
    "description": (
        "List adherence exceptions in a date window: late starts, missed "
        "breaks, early outs, extended breaks, unplanned aux, no-shows. Use "
        "when the user asks 'show me exceptions', 'who was late today', "
        "'what missed breaks happened this week'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
            "exception_type": {
                "type": "string",
                "enum": [
                    "late_start",
                    "missed_break",
                    "early_out",
                    "extended_break",
                    "unplanned_aux",
                    "no_show",
                ],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    start_date = _parse_date_arg(db, args.get("start_date"))
    end_date = (
        _parse_date_arg(db, args.get("end_date"))
        if args.get("end_date")
        else start_date
    )
    exception_type: str | None = args.get("exception_type")
    limit: int = int(args.get("limit") or 25)

    start_ts = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end_ts = datetime.combine(
        end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )

    params: dict[str, Any] = {"start": start_ts, "end": end_ts, "limit": limit}
    type_clause = ""
    if exception_type:
        type_clause = "AND ex.exception_type = :etype"
        params["etype"] = exception_type

    rows = (
        db.execute(
            text(
                f"""
                SELECT a.full_name, a.employee_id, ex.start_ts, ex.end_ts,
                       ex.exception_type, ex.duration_seconds,
                       ex.planned_state, ex.actual_state, ex.note
                FROM adherence_exceptions ex
                JOIN agents a ON a.id = ex.agent_id
                WHERE ex.start_ts >= :start AND ex.start_ts < :end
                {type_clause}
                ORDER BY ex.start_ts DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )

    table_rows = [
        [
            r["start_ts"].strftime("%Y-%m-%d %H:%M"),
            r["full_name"],
            r["employee_id"],
            r["exception_type"],
            f"{r['duration_seconds'] // 60}m{r['duration_seconds'] % 60:02d}s",
            f"{r['planned_state']} → {r['actual_state']}",
            r["note"] or "",
        ]
        for r in rows
    ]
    title = (
        f"Adherence exceptions — {start_date.isoformat()} to {end_date.isoformat()}"
        + (f" (type={exception_type})" if exception_type else "")
        + f" — {len(rows)} found"
    )
    return {
        "render": "table",
        "title": title,
        "columns": ["When", "Agent", "ID", "Type", "Duration", "Planned → Actual", "Note"],
        "rows": table_rows,
    }


def _parse_date_arg(db: Session, value: str | None) -> date:
    if value is None:
        return db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"].date()
    return date.fromisoformat(value)
