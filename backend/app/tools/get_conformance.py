"""
get_conformance — schedule conformance (actual vs scheduled hours).

Conformance is the volume-only sibling of adherence: did the agent
work the *right number of hours*, irrespective of whether they were in
the right state at the right time. Computed as
   conformance = (actual_productive_seconds) / (scheduled_productive_seconds)

where productive means planned=work and actual ∈ {available, on_call, acw}.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_conformance",
    "description": (
        "Schedule conformance — actual productive hours / scheduled "
        "productive hours, per agent, for a date window. Use when the user "
        "asks 'are people working their scheduled hours' or 'who is over/under "
        "on hours this week'. Conformance ignores timing (use get_adherence "
        "for that) — it only asks 'did the hours show up'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
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
    limit = int(args.get("limit") or 25)

    start_ts = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end_ts = datetime.combine(
        end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )

    rows = (
        db.execute(
            text(
                """
                WITH scheduled AS (
                    SELECT seg.agent_id,
                           SUM(EXTRACT(EPOCH FROM (seg.end_time - seg.start_time))) AS sched_sec
                    FROM shift_segments seg
                    WHERE seg.start_time >= :start AND seg.start_time < :end
                      AND seg.segment_type = 'work'
                    GROUP BY seg.agent_id
                ),
                actuals AS (
                    SELECT ax.agent_id,
                           SUM(EXTRACT(EPOCH FROM (
                               LEAST(COALESCE(ax.end_ts, sim_now()), :end)
                               - GREATEST(ax.start_ts, :start)
                           ))) AS actual_sec
                    FROM agent_aux_events ax
                    WHERE ax.aux_code IN ('available','on_call','acw')
                      AND ax.start_ts < :end
                      AND COALESCE(ax.end_ts, sim_now()) > :start
                    GROUP BY ax.agent_id
                )
                SELECT a.full_name, a.employee_id,
                       COALESCE(scheduled.sched_sec, 0) AS sched_sec,
                       COALESCE(actuals.actual_sec, 0)  AS actual_sec
                FROM agents a
                LEFT JOIN scheduled ON scheduled.agent_id = a.id
                LEFT JOIN actuals   ON actuals.agent_id  = a.id
                WHERE a.active = TRUE AND COALESCE(scheduled.sched_sec, 0) > 0
                ORDER BY ABS(
                    COALESCE(actuals.actual_sec, 0) - COALESCE(scheduled.sched_sec, 0)
                ) DESC
                LIMIT :limit
                """
            ),
            {"start": start_ts, "end": end_ts, "limit": limit},
        )
        .mappings()
        .all()
    )
    table_rows = []
    for r in rows:
        sched = float(r["sched_sec"])
        actual = float(r["actual_sec"])
        pct = (actual / sched * 100) if sched else 0
        delta_min = (actual - sched) / 60
        table_rows.append(
            [
                r["full_name"],
                r["employee_id"],
                _hhmm(sched),
                _hhmm(actual),
                f"{pct:.1f}%",
                f"{delta_min:+.0f}m",
            ]
        )
    return {
        "render": "table",
        "title": f"Conformance — {start_date.isoformat()} to {end_date.isoformat()} (biggest deltas first)",
        "columns": ["Agent", "ID", "Scheduled", "Actual", "Conformance", "Delta"],
        "rows": table_rows,
    }


def _parse_date_arg(db: Session, value: str | None) -> date:
    if value is None:
        return db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"].date()
    return date.fromisoformat(value)


def _hhmm(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"
