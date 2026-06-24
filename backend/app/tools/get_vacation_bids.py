"""
get_vacation_bids — ranked vacation preferences for an agent or the whole round.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_vacation_bids",
    "description": (
        "Show submitted vacation bids (ranked week preferences) for a round — for "
        "one agent (by employee_id) or the whole round (most senior first). Use "
        "when the user asks 'what did Adams bid', 'show the bids for the round'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "round_id": {"type": "integer"},
            "employee_id": {"type": "string"},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    round_id = args.get("round_id")
    if round_id is None:
        round_id = db.execute(
            text("SELECT id FROM bid_rounds ORDER BY created_at DESC LIMIT 1")
        ).scalar_one_or_none()
        if round_id is None:
            return {"render": "error", "message": "No bid rounds exist yet.", "code": "NO_ROUND"}

    where = "WHERE b.round_id = :id"
    params: dict[str, Any] = {"id": int(round_id)}
    if args.get("employee_id"):
        where += " AND a.employee_id = :eid"
        params["eid"] = args["employee_id"]

    rows = (
        db.execute(
            text(
                f"""
                SELECT a.full_name, a.employee_id, a.hire_date, b.week_start, b.rank
                FROM vacation_bids b
                JOIN agents a ON a.id = b.agent_id
                {where}
                ORDER BY a.hire_date NULLS LAST, a.employee_id, b.rank
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    if not rows:
        return {
            "render": "error",
            "message": "No bids found for that round/agent.",
            "code": "NO_BIDS",
        }
    table_rows = [
        [r["full_name"], r["employee_id"], int(r["rank"]), r["week_start"].isoformat()]
        for r in rows
    ]
    return {
        "render": "table",
        "title": f"Vacation bids — round {round_id}" + (f", {args['employee_id']}" if args.get("employee_id") else ""),
        "columns": ["Agent", "ID", "Pref rank", "Week"],
        "rows": table_rows,
    }
