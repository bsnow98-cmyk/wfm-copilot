"""
get_team_kpis — aggregate KPIs for the whole team or one skill team.

Returns: headcount, avg adherence, avg QA, total exception minutes,
total scheduled hours, average tenure. Filters to a skill if asked.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.tools.get_adherence import ADHERENT_MATCH

definition: dict[str, Any] = {
    "name": "get_team_kpis",
    "description": (
        "Team-level KPIs — headcount, avg adherence, avg QA, exception "
        "minutes, scheduled hours, avg tenure. Filter to a single skill "
        "team via the `skill` arg. Use when the user asks 'how is the team "
        "doing', 'team KPIs', 'how is the sales team this week'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "Skill name; omit for org-wide."},
            "window_days": {"type": "integer", "minimum": 1, "maximum": 90},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    skill_name = args.get("skill")
    window_days = int(args.get("window_days") or 14)
    now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    start_ts = now - timedelta(days=window_days)

    where_skill = ""
    params: dict[str, Any] = {"start": start_ts, "end": now}
    if skill_name:
        where_skill = (
            "AND seg.agent_id IN ("
            " SELECT ask.agent_id FROM agent_skills ask "
            " JOIN skills s ON s.id = ask.skill_id WHERE s.name = :skill)"
        )
        params["skill"] = skill_name

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
                WHERE seg.start_time >= :start AND seg.start_time < :end
                  AND seg.segment_type <> 'off'
                  {where_skill}
                """
            ),
            params,
        )
        .mappings()
        .one()
    )
    where_skill_q = ""
    qa_params: dict[str, Any] = {"start": start_ts}
    if skill_name:
        where_skill_q = (
            "AND agent_id IN ("
            " SELECT ask.agent_id FROM agent_skills ask "
            " JOIN skills s ON s.id = ask.skill_id WHERE s.name = :skill)"
        )
        qa_params["skill"] = skill_name
    qa = (
        db.execute(
            text(
                f"""
                SELECT AVG(score) AS avg_score, COUNT(*) AS n
                FROM agent_qa_scores
                WHERE evaluated_at >= :start
                {where_skill_q}
                """
            ),
            qa_params,
        )
        .mappings()
        .one()
    )
    exc = (
        db.execute(
            text(
                f"""
                SELECT COUNT(*) AS n, SUM(duration_seconds) AS total_sec
                FROM adherence_exceptions ex
                WHERE ex.start_ts >= :start AND ex.start_ts < :end
                {where_skill_q.replace('agent_id', 'ex.agent_id')}
                """
            ),
            params,
        )
        .mappings()
        .one()
    )
    headcount = (
        db.execute(
            text(
                f"""
                SELECT COUNT(*) FROM agents a
                WHERE a.active = TRUE
                {'AND a.id IN (SELECT ask.agent_id FROM agent_skills ask JOIN skills s ON s.id = ask.skill_id WHERE s.name = :skill)' if skill_name else ''}
                """
            ),
            {"skill": skill_name} if skill_name else {},
        ).scalar()
        or 0
    )
    avg_tenure = (
        db.execute(
            text(
                f"""
                SELECT AVG(EXTRACT(EPOCH FROM (sim_now() - hire_date))) / 86400 / 365.25 AS yrs
                FROM agents a
                WHERE a.active = TRUE AND a.hire_date IS NOT NULL
                {'AND a.id IN (SELECT ask.agent_id FROM agent_skills ask JOIN skills s ON s.id = ask.skill_id WHERE s.name = :skill)' if skill_name else ''}
                """
            ),
            {"skill": skill_name} if skill_name else {},
        ).scalar()
        or 0
    )

    adh_pct = (
        float(adh["adherent_seconds"]) / float(adh["scheduled_seconds"]) * 100
        if adh["scheduled_seconds"]
        else 0
    )
    rows: list[list[Any]] = [
        ["Headcount", int(headcount)],
        ["Average tenure", f"{float(avg_tenure):.1f} yrs"],
        ["Scheduled hours", f"{int((adh['scheduled_seconds'] or 0)) // 3600}h"],
        ["Adherence", f"{adh_pct:.1f}%"],
        ["Exceptions (count)", int(exc["n"] or 0)],
        ["Exceptions (minutes)", f"{int((exc['total_sec'] or 0)) // 60}m"],
        ["QA average", f"{float(qa['avg_score'] or 0):.1f}"],
        ["QA evals", int(qa["n"] or 0)],
    ]
    return {
        "render": "table",
        "title": (
            f"Team KPIs — {'team: ' + skill_name if skill_name else 'org-wide'} — "
            f"last {window_days}d"
        ),
        "columns": ["Metric", "Value"],
        "rows": rows,
    }
