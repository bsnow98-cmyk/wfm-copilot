"""
get_realtime_alerts — what would be paging the WFM analyst right now.

Computed rules (no separate alerts table — derived at query time so we
can tune without a backfill):
  SL_RED      service_level < 0.7 in the current interval
  SL_YELLOW   service_level < 0.8 in the current interval
  ASA_RED     asa_seconds > 60 in the current interval
  UNDERSTAFFED  agents_available < required (from staffing_requirements)
  VOLUME_SPIKE  offered > 1.25 * forecast for this interval
  ABANDON_RATE  abandoned/offered > 0.05 in current interval

Each alert returns severity (low/medium/high), code, message, and the
context value that triggered it.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_realtime_alerts",
    "description": (
        "Real-time alerts that would be paging the WFM analyst right now — "
        "SL breaches, ASA breaches, understaffing, volume spikes, abandon "
        "rate. Use when the user asks 'any alerts', 'anything firing', "
        "'what's hot right now'."
    ),
    "input_schema": {"type": "object", "properties": {}},
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    interval_start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)

    iv = (
        db.execute(
            text(
                """
                SELECT SUM(offered) AS offered, SUM(handled) AS handled,
                       SUM(abandoned) AS abandoned, AVG(service_level) AS sl,
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
    fc = db.execute(
        text(
            """
            SELECT forecast_offered FROM forecast_intervals fi
            JOIN (SELECT id FROM forecast_runs ORDER BY created_at DESC LIMIT 1) fr
              ON fr.id = fi.forecast_run_id
            WHERE fi.interval_start = :start LIMIT 1
            """
        ),
        {"start": interval_start},
    ).scalar()

    available = (
        db.execute(
            text(
                """
                SELECT COUNT(DISTINCT agent_id) FROM agent_aux_events
                WHERE aux_code IN ('available','on_call','acw')
                  AND start_ts <= :now AND (end_ts IS NULL OR end_ts > :now)
                """
            ),
            {"now": now},
        ).scalar()
        or 0
    )
    required = (
        db.execute(
            text(
                """
                SELECT MAX(required_agents) FROM staffing_requirement_intervals
                WHERE interval_start = :start
                """
            ),
            {"start": interval_start},
        ).scalar()
        or 0
    )

    alerts: list[dict[str, Any]] = []
    sl = float(iv["sl"] or 0)
    asa = float(iv["asa"] or 0)
    offered = int(iv["offered"] or 0)
    abandoned = int(iv["abandoned"] or 0)

    if sl > 0 and sl < 0.7:
        alerts.append(("high", "SL_RED", f"Service level {sl*100:.1f}% (target 80%)"))
    elif sl > 0 and sl < 0.8:
        alerts.append(("medium", "SL_YELLOW", f"Service level {sl*100:.1f}% (target 80%)"))
    if asa > 60:
        alerts.append(("high" if asa > 90 else "medium", "ASA_RED", f"ASA {asa:.0f}s (target ≤30s)"))
    if required and available < required:
        alerts.append(("high", "UNDERSTAFFED", f"{available} available vs {required} required"))
    if fc and offered > 1.25 * float(fc):
        alerts.append(("medium", "VOLUME_SPIKE", f"Offered {offered} vs forecast {float(fc):.0f}"))
    if offered and abandoned / offered > 0.05:
        alerts.append(("high", "ABANDON_RATE", f"Abandon {abandoned/offered*100:.1f}% (>5%)"))

    rows = [[sev, code, msg] for (sev, code, msg) in alerts]
    return {
        "render": "table",
        "title": f"Real-time alerts — {now.strftime('%H:%M:%S')} (sim) — {len(alerts)} firing",
        "columns": ["Severity", "Code", "Message"],
        "rows": rows,
    }
