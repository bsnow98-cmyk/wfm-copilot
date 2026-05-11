"""
get_class_progress — chronological QA / adherence trend for a new-hire class.

Returns a chart.line with two series (avg QA, avg adherence) over the
class's evaluation timeline. Designed to read alongside
get_new_hire_progress (which gives the per-agent snapshot).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_class_progress",
    "description": (
        "Class-level progress trend — chart of avg QA score and avg "
        "adherence over the class's evaluation timeline. Use when the user "
        "asks 'class trend', 'how is the class progressing over time', "
        "'class QA over weeks'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "class_id": {"type": "integer"},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    class_id = args.get("class_id")
    if class_id is None:
        class_row = (
            db.execute(
                text(
                    "SELECT id, class_name FROM new_hire_classes "
                    "WHERE status = 'in_class' ORDER BY start_date DESC LIMIT 1"
                )
            )
            .mappings()
            .one_or_none()
        )
    else:
        class_row = (
            db.execute(
                text("SELECT id, class_name FROM new_hire_classes WHERE id = :id"),
                {"id": int(class_id)},
            )
            .mappings()
            .one_or_none()
        )
    if not class_row:
        return {"render": "error", "message": "Class not found.", "code": "NO_CLASS"}

    rows = (
        db.execute(
            text(
                """
                SELECT evaluated_at::date AS day,
                       AVG(qa_score) AS avg_qa,
                       AVG(adherence_pct) AS avg_adh
                FROM new_hire_progress
                WHERE class_id = :cid
                GROUP BY day
                ORDER BY day
                """
            ),
            {"cid": class_row["id"]},
        )
        .mappings()
        .all()
    )
    qa_points = [{"x": r["day"].isoformat(), "y": round(float(r["avg_qa"] or 0), 1)} for r in rows]
    adh_points = [
        {"x": r["day"].isoformat(), "y": round(float(r["avg_adh"] or 0) * 100, 1)}
        for r in rows
    ]
    return {
        "render": "chart.line",
        "title": f"Class progress — {class_row['class_name']}",
        "yLabel": "score / %",
        "series": [
            {"name": "Avg QA", "points": qa_points},
            {"name": "Avg adherence %", "points": adh_points},
        ],
    }
