"""
get_adherence — strict adherence % over a date range.

Adherence = (seconds in adherent state) / (seconds scheduled to be in a
graded state). "Graded states" = work, break, lunch, training (NOT off).

Match rule (kept in sync with migrations/0014_adherence.sql):
  planned=work     → actual in {available, on_call, acw}            adherent
  planned=break    → actual=break                                   adherent
  planned=lunch    → actual=lunch                                   adherent
  planned=training → actual in {training, meeting, coaching}        adherent
  planned=off      → not graded

Implemented as a single SQL pass that:
  1. Joins shift_segments (planned) ← LATERAL → agent_aux_events (actual)
  2. Computes overlap seconds with each actual event
  3. Compares aux_code to the planned segment_type and sums

Returns either a per-agent table or a chronological chart.line, depending
on aggregation arg.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_adherence",
    "description": (
        "Schedule adherence — strict, per-second comparison of planned vs "
        "actual aux state. Use when the user asks 'how is adherence', "
        "'who's out of adherence today', or wants adherence trends over a "
        "date range. Aggregation=daily returns a chart.line; aggregation="
        "agent returns a ranked table of agents for the given window."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start_date": {
                "type": "string",
                "description": "ISO YYYY-MM-DD. Defaults to today (sim-now).",
            },
            "end_date": {
                "type": "string",
                "description": "ISO YYYY-MM-DD inclusive. Defaults to start_date.",
            },
            "aggregation": {
                "type": "string",
                "enum": ["daily", "agent"],
                "description": (
                    "daily = chart.line of adherence % per day; "
                    "agent = table ranked by lowest adherence in window."
                ),
            },
        },
    },
}

ADHERENT_MATCH = """
    (
        (seg.segment_type = 'work'     AND ax.aux_code IN ('available','on_call','acw')) OR
        (seg.segment_type = 'break'    AND ax.aux_code = 'break') OR
        (seg.segment_type = 'lunch'    AND ax.aux_code = 'lunch') OR
        (seg.segment_type = 'training' AND ax.aux_code IN ('training','meeting','coaching'))
    )
"""

_OVERLAP_CTE = """
    WITH adh_overlaps AS (
        SELECT
            seg.agent_id,
            seg.segment_type AS planned,
            ax.aux_code      AS actual,
            seg.start_time::date AS day,
            EXTRACT(EPOCH FROM (
                LEAST(seg.end_time, ax.end_ts) - GREATEST(seg.start_time, ax.start_ts)
            )) AS overlap_seconds,
            """ + ADHERENT_MATCH + """ AS is_adherent
        FROM shift_segments seg
        JOIN agent_aux_events ax
          ON ax.agent_id = seg.agent_id
         AND ax.start_ts < seg.end_time
         AND COALESCE(ax.end_ts, sim_now()) > seg.start_time
        WHERE seg.start_time >= :start AND seg.start_time < :end
          AND seg.segment_type <> 'off'
    )
"""


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    start_date = _parse_date_arg(db, args.get("start_date"))
    end_date = _parse_date_arg(db, args.get("end_date")) if args.get("end_date") else start_date
    if end_date < start_date:
        return {"render": "error", "message": "end_date before start_date", "code": "BAD_RANGE"}
    aggregation = args.get("aggregation") or "daily"

    start_ts = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end_ts = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)

    if aggregation == "agent":
        sql = _OVERLAP_CTE + """
            SELECT a.full_name, a.employee_id,
                   SUM(overlap_seconds) AS scheduled_seconds,
                   SUM(CASE WHEN is_adherent THEN overlap_seconds ELSE 0 END) AS adherent_seconds
            FROM adh_overlaps o
            JOIN agents a ON a.id = o.agent_id
            GROUP BY a.full_name, a.employee_id
            HAVING SUM(overlap_seconds) > 0
            ORDER BY (SUM(CASE WHEN is_adherent THEN overlap_seconds ELSE 0 END)
                       / NULLIF(SUM(overlap_seconds), 0)) ASC
            LIMIT 25
        """
        rows = db.execute(text(sql), {"start": start_ts, "end": end_ts}).mappings().all()
        table_rows = [
            [
                r["full_name"],
                r["employee_id"],
                f"{r['adherent_seconds']/r['scheduled_seconds']*100:.1f}%",
                _hhmm(r["scheduled_seconds"]),
            ]
            for r in rows
        ]
        return {
            "render": "table",
            "title": f"Adherence by agent — {start_date.isoformat()} to {end_date.isoformat()} (lowest first)",
            "columns": ["Agent", "ID", "Adherence", "Scheduled"],
            "rows": table_rows,
        }

    # daily
    sql = _OVERLAP_CTE + """
        SELECT day,
               SUM(overlap_seconds) AS scheduled_seconds,
               SUM(CASE WHEN is_adherent THEN overlap_seconds ELSE 0 END) AS adherent_seconds
        FROM adh_overlaps
        GROUP BY day
        ORDER BY day
    """
    rows = db.execute(text(sql), {"start": start_ts, "end": end_ts}).mappings().all()
    points = [
        {
            "x": r["day"].isoformat(),
            "y": round(float(r["adherent_seconds"]) / float(r["scheduled_seconds"]) * 100, 2),
        }
        for r in rows
        if r["scheduled_seconds"]
    ]
    return {
        "render": "chart.line",
        "title": f"Adherence % — {start_date.isoformat()} to {end_date.isoformat()}",
        "yLabel": "%",
        "series": [{"name": "Adherence", "points": points}],
    }


def _parse_date_arg(db: Session, value: str | None) -> date:
    if value is None:
        row = db.execute(text("SELECT sim_now() AS ts")).mappings().one()
        return row["ts"].date()
    return date.fromisoformat(value)


def _hhmm(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"
