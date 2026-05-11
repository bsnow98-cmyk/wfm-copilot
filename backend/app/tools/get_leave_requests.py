"""
get_leave_requests — list leave requests with filters.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_leave_requests",
    "description": (
        "List leave requests — defaults to pending requests in the next 30 "
        "days. Filter by status (pending/approved/denied/cancelled) or "
        "leave_type (PTO/sick/unpaid/swap). Use when the user asks 'who "
        "has PTO pending', 'show me leave requests', 'what's in the queue "
        "for approval'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "approved", "denied", "cancelled"],
            },
            "leave_type": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    status = args.get("status") or "pending"
    leave_type = args.get("leave_type")
    limit = int(args.get("limit") or 25)

    params: dict[str, Any] = {"status": status, "limit": limit}
    type_clause = ""
    if leave_type:
        type_clause = "AND lr.leave_type = :leave_type"
        params["leave_type"] = leave_type

    rows = (
        db.execute(
            text(
                f"""
                SELECT a.full_name, a.employee_id, lr.start_ts, lr.end_ts,
                       lr.leave_type, lr.status, lr.reason,
                       EXTRACT(EPOCH FROM (lr.end_ts - lr.start_ts)) / 3600 AS hours
                FROM leave_requests lr
                JOIN agents a ON a.id = lr.agent_id
                WHERE lr.status = :status
                {type_clause}
                ORDER BY lr.start_ts ASC
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
            r["full_name"],
            r["employee_id"],
            r["leave_type"],
            r["start_ts"].strftime("%Y-%m-%d"),
            r["end_ts"].strftime("%Y-%m-%d"),
            f"{float(r['hours']):.0f}h",
            r["reason"] or "",
        ]
        for r in rows
    ]
    title = (
        f"Leave requests — status={status}"
        + (f", type={leave_type}" if leave_type else "")
        + f" — {len(rows)} found"
    )
    return {
        "render": "table",
        "title": title,
        "columns": ["Agent", "ID", "Type", "Start", "End", "Hours", "Reason"],
        "rows": table_rows,
    }
