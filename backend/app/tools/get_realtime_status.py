"""
get_realtime_status — what's happening right now (sim-now).

Reads the live ticker (`sim_now()`) and reports KPIs *as of right now*:
- agents on schedule (planned to be working)
- agents available (actual)
- queue intake rate (interval_history of the current interval)
- service level + ASA of the current 30-min interval
- delta vs forecast for the same interval
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_realtime_status",
    "description": (
        "Real-time snapshot: SL, ASA, queue volume, agents scheduled vs "
        "available *as of right now*. Use when the user asks 'how are we "
        "doing right now', 'real-time', 'live status', 'what's the floor "
        "look like'."
    ),
    "input_schema": {"type": "object", "properties": {}},
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]

    # Round to the start of the 30-min interval.
    interval_start = now.replace(
        minute=(now.minute // 30) * 30, second=0, microsecond=0
    )
    interval_end = interval_start + timedelta(minutes=30)

    scheduled = (
        db.execute(
            text(
                """
                SELECT COUNT(DISTINCT seg.agent_id) AS n
                FROM shift_segments seg
                WHERE seg.segment_type = 'work'
                  AND seg.start_time <= :now AND seg.end_time > :now
                """
            ),
            {"now": now},
        )
        .scalar()
        or 0
    )
    available = (
        db.execute(
            text(
                """
                SELECT COUNT(DISTINCT ax.agent_id) AS n
                FROM agent_aux_events ax
                WHERE ax.aux_code IN ('available','on_call','acw')
                  AND ax.start_ts <= :now
                  AND (ax.end_ts IS NULL OR ax.end_ts > :now)
                """
            ),
            {"now": now},
        )
        .scalar()
        or 0
    )
    on_aux = (
        db.execute(
            text(
                """
                SELECT COUNT(DISTINCT ax.agent_id) AS n
                FROM agent_aux_events ax
                WHERE ax.aux_code NOT IN ('available','on_call','acw','offline')
                  AND ax.start_ts <= :now
                  AND (ax.end_ts IS NULL OR ax.end_ts > :now)
                """
            ),
            {"now": now},
        )
        .scalar()
        or 0
    )

    interval = (
        db.execute(
            text(
                """
                SELECT SUM(offered) AS offered,
                       SUM(handled) AS handled,
                       SUM(abandoned) AS abandoned,
                       AVG(service_level) AS sl,
                       AVG(asa_seconds) AS asa
                FROM interval_history
                WHERE interval_start = :start
                """
            ),
            {"start": interval_start},
        )
        .mappings()
        .one()
    )
    forecast = (
        db.execute(
            text(
                """
                SELECT forecast_offered
                FROM forecast_intervals fi
                JOIN (
                    SELECT id FROM forecast_runs ORDER BY created_at DESC LIMIT 1
                ) fr ON fr.id = fi.forecast_run_id
                WHERE fi.interval_start = :start
                LIMIT 1
                """
            ),
            {"start": interval_start},
        )
        .scalar()
    )

    offered = int(interval["offered"] or 0)
    handled = int(interval["handled"] or 0)
    abandoned = int(interval["abandoned"] or 0)
    sl = float(interval["sl"] or 0) * 100
    asa = float(interval["asa"] or 0)
    fc = float(forecast or 0)
    fc_delta_pct = ((offered - fc) / fc * 100) if fc else 0

    cols = ["Metric", "Value"]
    rows: list[list[Any]] = [
        ["Time (sim-now)", now.strftime("%Y-%m-%d %H:%M:%S UTC")],
        ["Interval", f"{interval_start.strftime('%H:%M')}–{interval_end.strftime('%H:%M')}"],
        ["Agents scheduled (now)", scheduled],
        ["Agents available (now)", available],
        ["Agents on aux (now)", on_aux],
        ["Offered (this interval)", offered],
        ["Handled (this interval)", handled],
        ["Abandoned (this interval)", abandoned],
        ["Service level (this interval)", f"{sl:.1f}%"],
        ["ASA (this interval)", f"{asa:.0f}s"],
        ["Volume vs forecast", f"{fc_delta_pct:+.1f}%" if fc else "—"],
    ]
    return {
        "render": "table",
        "title": f"Real-time status — {now.strftime('%H:%M:%S')} (sim)",
        "columns": cols,
        "rows": rows,
    }
