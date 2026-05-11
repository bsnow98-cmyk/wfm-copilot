"""
rank_agents — top/bottom N agents on a chosen metric.

Supported metrics:
  adherence       (highest first by default)
  qa              (highest first)
  exceptions      (most-exception-minutes first — i.e. "worst" first)
  tenure          (longest first)
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.tools.get_adherence import ADHERENT_MATCH

definition: dict[str, Any] = {
    "name": "rank_agents",
    "description": (
        "Rank agents on a metric (adherence, qa, exceptions, tenure). Use "
        "when the user asks 'top 10 by adherence', 'bottom 5 QA scores', "
        "'who has the most exceptions this week', 'most tenured agents'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "enum": ["adherence", "qa", "exceptions", "tenure"],
            },
            "order": {"type": "string", "enum": ["desc", "asc"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "window_days": {"type": "integer", "minimum": 1, "maximum": 90},
        },
        "required": ["metric"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    metric: str = args["metric"]
    order: str = args.get("order") or "desc"
    limit = int(args.get("limit") or 10)
    window_days = int(args.get("window_days") or 14)
    now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    start_ts = now - timedelta(days=window_days)

    if metric == "adherence":
        rows = (
            db.execute(
                text(
                    f"""
                    SELECT a.full_name, a.employee_id,
                           SUM(EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
                                          - GREATEST(seg.start_time, ax.start_ts))))
                                AS scheduled_seconds,
                           SUM(CASE WHEN {ADHERENT_MATCH} THEN
                               EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
                                          - GREATEST(seg.start_time, ax.start_ts)))
                           ELSE 0 END) AS adherent_seconds
                    FROM shift_segments seg
                    JOIN agent_aux_events ax ON ax.agent_id = seg.agent_id
                     AND ax.start_ts < seg.end_time
                     AND COALESCE(ax.end_ts, sim_now()) > seg.start_time
                    JOIN agents a ON a.id = seg.agent_id
                    WHERE seg.start_time >= :start AND seg.start_time < :end
                      AND seg.segment_type <> 'off'
                    GROUP BY a.full_name, a.employee_id
                    HAVING SUM(EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
                                        - GREATEST(seg.start_time, ax.start_ts)))) > 0
                    ORDER BY (SUM(CASE WHEN {ADHERENT_MATCH} THEN
                                  EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
                                          - GREATEST(seg.start_time, ax.start_ts)))
                              ELSE 0 END)
                              / NULLIF(SUM(EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
                                          - GREATEST(seg.start_time, ax.start_ts)))), 0))
                              {'DESC' if order == 'desc' else 'ASC'}
                    LIMIT :limit
                    """
                ),
                {"start": start_ts, "end": now, "limit": limit},
            )
            .mappings()
            .all()
        )
        table_rows = [
            [
                i,
                r["full_name"],
                r["employee_id"],
                f"{float(r['adherent_seconds'])/float(r['scheduled_seconds'])*100:.1f}%",
            ]
            for i, r in enumerate(rows, start=1)
        ]
        return {
            "render": "table",
            "title": f"Top {len(rows)} by adherence ({order}) — last {window_days}d",
            "columns": ["Rank", "Agent", "ID", "Adherence"],
            "rows": table_rows,
        }

    if metric == "qa":
        rows = (
            db.execute(
                text(
                    f"""
                    SELECT a.full_name, a.employee_id, AVG(q.score) AS avg_score,
                           COUNT(*) AS n
                    FROM agent_qa_scores q
                    JOIN agents a ON a.id = q.agent_id
                    WHERE q.evaluated_at >= :start
                    GROUP BY a.full_name, a.employee_id
                    ORDER BY avg_score {'DESC' if order == 'desc' else 'ASC'}
                    LIMIT :limit
                    """
                ),
                {"start": start_ts, "limit": limit},
            )
            .mappings()
            .all()
        )
        table_rows = [
            [i, r["full_name"], r["employee_id"], f"{float(r['avg_score']):.1f}", int(r["n"])]
            for i, r in enumerate(rows, start=1)
        ]
        return {
            "render": "table",
            "title": f"Top {len(rows)} by QA score ({order}) — last {window_days}d",
            "columns": ["Rank", "Agent", "ID", "Avg QA", "Evals"],
            "rows": table_rows,
        }

    if metric == "exceptions":
        rows = (
            db.execute(
                text(
                    f"""
                    SELECT a.full_name, a.employee_id, COUNT(*) AS n,
                           SUM(duration_seconds) AS total_sec
                    FROM adherence_exceptions ex
                    JOIN agents a ON a.id = ex.agent_id
                    WHERE ex.start_ts >= :start
                    GROUP BY a.full_name, a.employee_id
                    ORDER BY total_sec {'DESC' if order == 'desc' else 'ASC'}
                    LIMIT :limit
                    """
                ),
                {"start": start_ts, "limit": limit},
            )
            .mappings()
            .all()
        )
        table_rows = [
            [
                i,
                r["full_name"],
                r["employee_id"],
                int(r["n"]),
                f"{int(r['total_sec']) // 60}m",
            ]
            for i, r in enumerate(rows, start=1)
        ]
        return {
            "render": "table",
            "title": f"Top {len(rows)} by exception minutes ({order}) — last {window_days}d",
            "columns": ["Rank", "Agent", "ID", "Count", "Total"],
            "rows": table_rows,
        }

    # tenure
    rows = (
        db.execute(
            text(
                f"""
                SELECT full_name, employee_id, hire_date
                FROM agents
                WHERE active = TRUE AND hire_date IS NOT NULL
                ORDER BY hire_date {'ASC' if order == 'desc' else 'DESC'}
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
        .mappings()
        .all()
    )
    today = now.date()
    table_rows = [
        [
            i,
            r["full_name"],
            r["employee_id"],
            r["hire_date"].isoformat(),
            f"{(today - r['hire_date']).days / 365.25:.1f} yrs",
        ]
        for i, r in enumerate(rows, start=1)
    ]
    return {
        "render": "table",
        "title": f"Top {len(rows)} by tenure ({order})",
        "columns": ["Rank", "Agent", "ID", "Hire date", "Tenure"],
        "rows": table_rows,
    }
