"""
get_anomalies tool — Phase 5 cherry-pick B preview. Ships against an empty
table until Phase 5 fills it; the chat path is wired now so no rewire later.

The anomalies table is created by Phase 5's migration; if it doesn't exist
yet we return an empty table (not an error) — that's the shipping behavior
on day 1 of Phase 6.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_anomalies",
    "description": (
        "List anomalies detected over the last N days, optionally filtered by "
        "queue. Returns a table with id, date, queue, category, severity, score. "
        "Use when the user asks 'why is X off' or 'anything weird recently'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "since_date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD; include anomalies on or after this date. Defaults to 7 days ago.",
            },
            "queue": {
                "type": "string",
                "description": "Optional queue filter.",
            },
        },
    },
}

_COLUMNS = ["id", "date", "queue", "category", "severity", "score"]


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    since = _parse_date(args.get("since_date"))
    queue: str | None = args.get("queue")

    where = "WHERE date >= :since"
    params: dict[str, Any] = {"since": since}
    if queue:
        where += " AND queue = :queue"
        params["queue"] = queue

    sql = f"""
        SELECT id, date, queue, category, severity, score
        FROM anomalies
        {where}
        ORDER BY date DESC, severity DESC
        LIMIT 100
    """

    try:
        result = db.execute(text(sql), params).all()
    except ProgrammingError:
        # Table doesn't exist yet (Phase 5 hasn't shipped). Return empty table.
        db.rollback()
        return {
            "render": "table",
            "title": "Anomalies",
            "columns": _COLUMNS,
            "rows": [],
        }

    rows: list[list[Any]] = [
        [
            r[0],
            r[1].isoformat() if hasattr(r[1], "isoformat") else r[1],
            r[2],
            r[3],
            r[4],
            float(r[5]) if r[5] is not None else None,
        ]
        for r in result
    ]

    return {
        "render": "table",
        "title": f"Anomalies since {since.isoformat()}"
        + (f" — {queue}" if queue else ""),
        "columns": _COLUMNS,
        "rows": rows,
    }


def _parse_date(value: str | None) -> date:
    if value is None:
        return (datetime.now(timezone.utc) - timedelta(days=7)).date()
    return date.fromisoformat(value)
