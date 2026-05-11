"""
get_new_hire_progress — class status + per-agent nesting progress.

Pulls the most-recent in-progress class, lists each cohort member's
latest nesting evaluation, and flags status (on_track / watch /
at_risk / washed_out).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_new_hire_progress",
    "description": (
        "New-hire class progress — class info plus per-agent latest nesting "
        "evaluation (week, QA, AHT, adherence, status). Use when the user "
        "asks 'how is the new hire class doing', 'nesting progress', "
        "'who's at risk in the class'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "class_id": {"type": "integer", "description": "Defaults to most-recent in-progress class."}
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    class_id = args.get("class_id")
    if class_id is None:
        class_row = (
            db.execute(
                text(
                    "SELECT id, class_name, start_date, end_date, target_size "
                    "FROM new_hire_classes WHERE status = 'in_class' "
                    "ORDER BY start_date DESC LIMIT 1"
                )
            )
            .mappings()
            .one_or_none()
        )
    else:
        class_row = (
            db.execute(
                text(
                    "SELECT id, class_name, start_date, end_date, target_size "
                    "FROM new_hire_classes WHERE id = :id"
                ),
                {"id": int(class_id)},
            )
            .mappings()
            .one_or_none()
        )
    if not class_row:
        return {"render": "error", "message": "No in-progress new-hire class.", "code": "NO_CLASS"}

    members = (
        db.execute(
            text(
                """
                SELECT a.full_name, a.employee_id, p.nesting_week, p.qa_score,
                       p.aht_seconds, p.adherence_pct, p.status, p.evaluated_at
                FROM new_hire_progress p
                JOIN agents a ON a.id = p.agent_id
                WHERE p.class_id = :cid
                  AND p.evaluated_at = (
                      SELECT MAX(evaluated_at) FROM new_hire_progress
                      WHERE class_id = :cid AND agent_id = p.agent_id
                  )
                ORDER BY p.status, p.qa_score DESC
                """
            ),
            {"cid": class_row["id"]},
        )
        .mappings()
        .all()
    )
    table_rows = [
        [
            m["full_name"],
            m["employee_id"],
            m["nesting_week"],
            f"{float(m['qa_score'] or 0):.1f}",
            f"{int(m['aht_seconds'] or 0)}s",
            f"{float(m['adherence_pct'] or 0) * 100:.1f}%",
            m["status"],
        ]
        for m in members
    ]
    return {
        "render": "table",
        "title": (
            f"Class: {class_row['class_name']} "
            f"({class_row['start_date']}→{class_row['end_date']}, target {class_row['target_size']}) "
            f"— {len(members)} members"
        ),
        "columns": ["Agent", "ID", "Week", "QA", "AHT", "Adherence", "Status"],
        "rows": table_rows,
    }
