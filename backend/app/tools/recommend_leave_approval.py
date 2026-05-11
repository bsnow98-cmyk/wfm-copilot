"""
recommend_leave_approval — auto-recommend approve/deny for pending leave.

Walks pending leave_requests in the next 30 days and runs the
check_leave_feasibility logic per request. Outputs a ranked table:
APPROVE (margin ≥ 2 every day), HOLD (any day margin in [0,1]),
DENY (any day margin < 0).

Policy is named in the title so it's auditable + swappable.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "recommend_leave_approval",
    "description": (
        "Recommend approve/hold/deny for each pending leave request based "
        "on per-day staffing margin (after the would-be approval). Use when "
        "the user asks 'which leave requests should I approve', 'work the "
        "PTO queue', 'auto-recommend leave decisions'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "horizon_days": {"type": "integer", "minimum": 7, "maximum": 90}
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    horizon_days = int(args.get("horizon_days") or 30)
    requests = (
        db.execute(
            text(
                """
                SELECT lr.id, lr.start_ts, lr.end_ts, lr.leave_type,
                       a.full_name, a.employee_id
                FROM leave_requests lr
                JOIN agents a ON a.id = lr.agent_id
                WHERE lr.status = 'pending'
                  AND lr.start_ts >= sim_now()
                  AND lr.start_ts < sim_now() + (:h || ' days')::interval
                ORDER BY lr.start_ts
                """
            ),
            {"h": horizon_days},
        )
        .mappings()
        .all()
    )

    rows: list[list[Any]] = []
    for r in requests:
        days = (
            db.execute(
                text(
                    """
                    SELECT MAX(sr.required_agents) AS required,
                           MAX(sc.scheduled_agents) AS scheduled
                    FROM staffing_requirement_intervals sr
                    LEFT JOIN schedule_coverage sc
                      ON sc.interval_start = sr.interval_start
                    WHERE sr.interval_start >= :start AND sr.interval_start < :end
                    GROUP BY (sr.interval_start::date)
                    """
                ),
                {"start": r["start_ts"], "end": r["end_ts"]},
            )
            .mappings()
            .all()
        )
        worst_margin = None
        for d in days:
            margin = int((d["scheduled"] or 0) - 1 - (d["required"] or 0))
            worst_margin = margin if worst_margin is None else min(worst_margin, margin)
        if worst_margin is None:
            verdict = "HOLD"
            reason = "No staffing data for window"
        elif worst_margin < 0:
            verdict = "DENY"
            reason = f"Shortfall day in window (margin {worst_margin})"
        elif worst_margin <= 1:
            verdict = "HOLD"
            reason = f"Tight margin ({worst_margin}); needs analyst eyes"
        else:
            verdict = "APPROVE"
            reason = f"Comfortable margin ({worst_margin})"
        rows.append(
            [
                r["id"],
                r["full_name"],
                r["employee_id"],
                r["leave_type"],
                r["start_ts"].date().isoformat(),
                r["end_ts"].date().isoformat(),
                verdict,
                reason,
            ]
        )

    return {
        "render": "table",
        "title": (
            f"Leave-approval recommendations — next {horizon_days}d "
            f"(policy: margin≥2 APPROVE, 0-1 HOLD, <0 DENY) — {len(rows)} pending"
        ),
        "columns": ["Req ID", "Agent", "ID", "Type", "Start", "End", "Verdict", "Reason"],
        "rows": rows,
    }
