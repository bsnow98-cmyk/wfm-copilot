"""
explain_adherence_drop — root-cause a low-adherence day.

Given a date, compute that day's adherence and break it down by
exception type, then list the top-5 offending agents. Output is a
chart.bar plus a sentence-level summary baked into the title.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.tools.get_adherence import ADHERENT_MATCH

definition: dict[str, Any] = {
    "name": "explain_adherence_drop",
    "description": (
        "Explain why adherence was low on a given day — bar chart of "
        "exception types by total minutes plus the day's overall "
        "adherence in the title. Use when the user asks 'why was "
        "adherence low yesterday', 'what caused the drop on <date>'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "ISO YYYY-MM-DD. Defaults to today (sim-now)."}
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    if args.get("date"):
        target = date.fromisoformat(args["date"])
    else:
        target = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"].date()

    start_ts = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
    end_ts = start_ts + timedelta(days=1)

    overall = (
        db.execute(
            text(
                f"""
                SELECT SUM(EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
                                              - GREATEST(seg.start_time, ax.start_ts))))
                       AS scheduled_seconds,
                       SUM(CASE WHEN {ADHERENT_MATCH} THEN
                           EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
                                              - GREATEST(seg.start_time, ax.start_ts)))
                       ELSE 0 END) AS adherent_seconds
                FROM shift_segments seg
                JOIN agent_aux_events ax
                  ON ax.agent_id = seg.agent_id
                 AND ax.start_ts < seg.end_time
                 AND COALESCE(ax.end_ts, sim_now()) > seg.start_time
                WHERE seg.start_time >= :start AND seg.start_time < :end
                  AND seg.segment_type <> 'off'
                """
            ),
            {"start": start_ts, "end": end_ts},
        )
        .mappings()
        .one()
    )
    if not overall["scheduled_seconds"]:
        return {
            "render": "error",
            "message": f"No scheduled time on {target.isoformat()}.",
            "code": "NO_SCHEDULE",
        }
    adherence_pct = float(overall["adherent_seconds"]) / float(overall["scheduled_seconds"]) * 100

    by_type = (
        db.execute(
            text(
                """
                SELECT exception_type, SUM(duration_seconds) AS total_seconds
                FROM adherence_exceptions
                WHERE start_ts >= :start AND start_ts < :end
                GROUP BY exception_type
                ORDER BY total_seconds DESC
                """
            ),
            {"start": start_ts, "end": end_ts},
        )
        .mappings()
        .all()
    )
    bars = [
        {"label": r["exception_type"], "value": round(float(r["total_seconds"]) / 60, 1)}
        for r in by_type
    ]
    if not bars:
        return {
            "render": "text",
            "content": (
                f"Adherence on {target.isoformat()} was {adherence_pct:.1f}% "
                "with zero recorded exceptions — drop, if any, was within tolerance."
            ),
        }
    return {
        "render": "chart.bar",
        "title": (
            f"Adherence drop drivers — {target.isoformat()} "
            f"(overall {adherence_pct:.1f}%) — minutes lost by exception type"
        ),
        "bars": bars,
    }
