"""
get_agent_performance — single-agent KPI snapshot.

Returns a vertical KPI table for one agent (by employee_id) over a
window: adherence, conformance, QA average, exception count, tenure,
skills + proficiencies. Designed for the "tell me about <agent>" ask.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.tools.get_adherence import ADHERENT_MATCH

definition: dict[str, Any] = {
    "name": "get_agent_performance",
    "description": (
        "Single-agent KPI snapshot — adherence, conformance, QA, exception "
        "count, tenure, skills. Use when the user asks 'tell me about "
        "<agent>', 'how is <name> doing', 'pull up <employee_id>'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "employee_id": {"type": "string"},
            "window_days": {"type": "integer", "minimum": 1, "maximum": 90},
        },
        "required": ["employee_id"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    eid = args["employee_id"]
    window_days = int(args.get("window_days") or 14)
    now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    start_ts = now - timedelta(days=window_days)

    agent = (
        db.execute(
            text(
                "SELECT id, full_name, employee_id, hire_date, "
                "       contracted_hours_per_week, timezone "
                "FROM agents WHERE employee_id = :eid"
            ),
            {"eid": eid},
        )
        .mappings()
        .one_or_none()
    )
    if not agent:
        return {"render": "error", "message": "Agent not found.", "code": "NO_AGENT"}

    adh = (
        db.execute(
            text(
                f"""
                SELECT SUM(EXTRACT(EPOCH FROM (LEAST(seg.end_time, ax.end_ts)
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
                WHERE seg.agent_id = :aid
                  AND seg.start_time >= :start AND seg.start_time < :end
                  AND seg.segment_type <> 'off'
                """
            ),
            {"aid": agent["id"], "start": start_ts, "end": now},
        )
        .mappings()
        .one()
    )
    adh_pct = (
        float(adh["adherent_seconds"]) / float(adh["scheduled_seconds"]) * 100
        if adh["scheduled_seconds"]
        else 0
    )

    excs = (
        db.execute(
            text(
                """
                SELECT COUNT(*) AS n, SUM(duration_seconds) AS total_sec
                FROM adherence_exceptions
                WHERE agent_id = :aid AND start_ts >= :start AND start_ts < :end
                """
            ),
            {"aid": agent["id"], "start": start_ts, "end": now},
        )
        .mappings()
        .one()
    )
    qa = (
        db.execute(
            text(
                """
                SELECT AVG(score) AS avg_score, COUNT(*) AS n
                FROM agent_qa_scores
                WHERE agent_id = :aid AND evaluated_at >= :start
                """
            ),
            {"aid": agent["id"], "start": start_ts},
        )
        .mappings()
        .one()
    )
    pto_bal = db.execute(
        text(
            "SELECT balance_after FROM pto_ledger WHERE agent_id = :aid "
            "ORDER BY event_ts DESC LIMIT 1"
        ),
        {"aid": agent["id"]},
    ).scalar()
    skills = (
        db.execute(
            text(
                "SELECT s.name, ask.proficiency FROM agent_skills ask "
                "JOIN skills s ON s.id = ask.skill_id WHERE ask.agent_id = :aid "
                "ORDER BY ask.proficiency DESC"
            ),
            {"aid": agent["id"]},
        )
        .mappings()
        .all()
    )

    tenure_yrs = (
        round((now.date() - agent["hire_date"]).days / 365.25, 1)
        if agent["hire_date"]
        else None
    )

    rows: list[list[Any]] = [
        ["Name", agent["full_name"]],
        ["Employee ID", agent["employee_id"]],
        ["Tenure", f"{tenure_yrs} yrs" if tenure_yrs is not None else "—"],
        ["Hours/week", f"{float(agent['contracted_hours_per_week'] or 0):.1f}"],
        ["Timezone", agent["timezone"]],
        ["Adherence (window)", f"{adh_pct:.1f}%"],
        ["Exceptions (count)", int(excs["n"] or 0)],
        ["Exceptions (minutes)", f"{int(excs['total_sec'] or 0) // 60}m"],
        [
            "QA average (window)",
            f"{float(qa['avg_score']):.1f} ({int(qa['n'])} evals)"
            if qa["avg_score"]
            else "—",
        ],
        ["PTO balance", f"{float(pto_bal or 0):.1f}h"],
        ["Skills", ", ".join(f"{s['name']}(L{s['proficiency']})" for s in skills) or "—"],
    ]
    return {
        "render": "table",
        "title": f"Agent performance — {agent['full_name']} (last {window_days}d)",
        "columns": ["Metric", "Value"],
        "rows": rows,
    }
