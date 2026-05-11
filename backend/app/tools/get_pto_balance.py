"""
get_pto_balance — per-agent or whole-team PTO balances.

Reads the most recent pto_ledger row per agent and returns balance,
plus year-to-date used (informational).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_pto_balance",
    "description": (
        "PTO balances — for one agent (by employee_id) or whole team "
        "(top 25 by balance descending). Use when the user asks 'how much "
        "PTO does <name> have', 'who has the most PTO banked', 'PTO "
        "balances'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "employee_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    employee_id = args.get("employee_id")
    limit = int(args.get("limit") or 25)

    if employee_id:
        rows = (
            db.execute(
                text(
                    """
                    SELECT a.full_name, a.employee_id,
                           (SELECT balance_after FROM pto_ledger
                             WHERE agent_id = a.id ORDER BY event_ts DESC LIMIT 1) AS balance,
                           (SELECT -SUM(hours) FROM pto_ledger
                             WHERE agent_id = a.id AND event_type = 'use'
                               AND event_ts >= date_trunc('year', sim_now())) AS used_ytd
                    FROM agents a
                    WHERE a.employee_id = :eid
                    """
                ),
                {"eid": employee_id},
            )
            .mappings()
            .all()
        )
    else:
        rows = (
            db.execute(
                text(
                    """
                    SELECT a.full_name, a.employee_id,
                           (SELECT balance_after FROM pto_ledger
                             WHERE agent_id = a.id ORDER BY event_ts DESC LIMIT 1) AS balance,
                           (SELECT -SUM(hours) FROM pto_ledger
                             WHERE agent_id = a.id AND event_type = 'use'
                               AND event_ts >= date_trunc('year', sim_now())) AS used_ytd
                    FROM agents a
                    WHERE a.active = TRUE
                    ORDER BY balance DESC NULLS LAST
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
            .mappings()
            .all()
        )

    table_rows = [
        [
            r["full_name"],
            r["employee_id"],
            f"{float(r['balance'] or 0):.1f}h",
            f"{float(r['used_ytd'] or 0):.1f}h",
        ]
        for r in rows
    ]
    title = (
        f"PTO balance — {employee_id}"
        if employee_id
        else f"PTO balances (top {len(rows)} by balance)"
    )
    return {
        "render": "table",
        "title": title,
        "columns": ["Agent", "ID", "Balance", "Used YTD"],
        "rows": table_rows,
    }
