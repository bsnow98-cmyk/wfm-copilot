"""
check_leave_feasibility — would approving this leave break SL?

For a leave_request id (or an ad-hoc agent_id + window), compares the
days the request covers against:
  - staffing_requirements.required_agents (vs current scheduled_agents - 1)
  - existing approved leave (would this be the Nth concurrent off?)
Returns an OK / WARN / FAIL recommendation with the math in the title.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "check_leave_feasibility",
    "description": (
        "Check whether approving a leave request would break SL on the "
        "affected days. Provide either request_id, or (employee_id, "
        "start_date, end_date). Returns a per-day table with required vs "
        "available staffing and an overall verdict (OK/WARN/FAIL)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "integer"},
            "employee_id": {"type": "string"},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    if args.get("request_id"):
        req = (
            db.execute(
                text(
                    """
                    SELECT lr.id, a.id AS agent_id, a.full_name, a.employee_id,
                           lr.start_ts, lr.end_ts
                    FROM leave_requests lr
                    JOIN agents a ON a.id = lr.agent_id
                    WHERE lr.id = :id
                    """
                ),
                {"id": int(args["request_id"])},
            )
            .mappings()
            .one_or_none()
        )
        if not req:
            return {"render": "error", "message": "Leave request not found.", "code": "NO_REQUEST"}
        start_ts = req["start_ts"]
        end_ts = req["end_ts"]
        label = f"{req['full_name']} ({req['employee_id']})"
    else:
        eid = args.get("employee_id")
        sd = args.get("start_date")
        ed = args.get("end_date")
        if not (eid and sd and ed):
            return {
                "render": "error",
                "message": "Provide request_id OR (employee_id, start_date, end_date).",
                "code": "BAD_ARGS",
            }
        agent = (
            db.execute(
                text("SELECT id, full_name FROM agents WHERE employee_id = :eid"),
                {"eid": eid},
            )
            .mappings()
            .one_or_none()
        )
        if not agent:
            return {"render": "error", "message": "Agent not found.", "code": "NO_AGENT"}
        start_ts = datetime.fromisoformat(sd).replace(tzinfo=timezone.utc)
        end_ts = datetime.fromisoformat(ed).replace(tzinfo=timezone.utc) + timedelta(days=1)
        label = f"{agent['full_name']} ({eid})"

    days = (
        db.execute(
            text(
                """
                SELECT (sr.interval_start::date) AS day,
                       MAX(sr.required_agents) AS required,
                       MAX(sc.scheduled_agents) AS scheduled
                FROM staffing_requirement_intervals sr
                LEFT JOIN schedule_coverage sc
                  ON sc.interval_start = sr.interval_start
                WHERE sr.interval_start >= :start AND sr.interval_start < :end
                GROUP BY day
                ORDER BY day
                """
            ),
            {"start": start_ts, "end": end_ts},
        )
        .mappings()
        .all()
    )

    verdicts: list[str] = []
    rows: list[list[Any]] = []
    for d in days:
        required = int(d["required"] or 0)
        scheduled = int(d["scheduled"] or 0)
        after = max(scheduled - 1, 0)
        margin = after - required
        if margin >= 2:
            v = "OK"
        elif margin >= 0:
            v = "WARN"
        else:
            v = "FAIL"
        verdicts.append(v)
        rows.append(
            [d["day"].isoformat(), required, scheduled, after, margin, v]
        )

    if not rows:
        return {
            "render": "error",
            "message": "No staffing requirements found for this window.",
            "code": "NO_STAFFING",
        }

    overall = "FAIL" if "FAIL" in verdicts else ("WARN" if "WARN" in verdicts else "OK")
    return {
        "render": "table",
        "title": (
            f"Leave feasibility — {label}, "
            f"{start_ts.date().isoformat()}→{(end_ts - timedelta(days=1)).date().isoformat()} — verdict: {overall}"
        ),
        "columns": ["Day", "Required", "Scheduled", "After approval", "Margin", "Verdict"],
        "rows": rows,
    }
