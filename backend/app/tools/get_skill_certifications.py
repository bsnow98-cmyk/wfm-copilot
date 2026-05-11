"""
get_skill_certifications — who's certified on what, expiring when.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_skill_certifications",
    "description": (
        "List skill certifications — filter by skill, by agent, or by "
        "upcoming expirations. Use when the user asks 'who is certified "
        "on billing', 'whose certs expire soon', 'cert status for <agent>'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {"type": "string"},
            "employee_id": {"type": "string"},
            "expiring_within_days": {"type": "integer", "minimum": 1, "maximum": 365},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    skill = args.get("skill")
    eid = args.get("employee_id")
    expiring = args.get("expiring_within_days")
    limit = int(args.get("limit") or 25)

    where: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if skill:
        where.append("s.name = :skill")
        params["skill"] = skill
    if eid:
        where.append("a.employee_id = :eid")
        params["eid"] = eid
    if expiring:
        where.append(
            "ac.expires_at IS NOT NULL "
            "AND ac.expires_at <= sim_now() + (:exp || ' days')::interval"
        )
        params["exp"] = int(expiring)

    where_sql = " AND ".join(where) if where else "TRUE"
    rows = (
        db.execute(
            text(
                f"""
                SELECT a.full_name, a.employee_id, s.name AS skill_name,
                       ac.level, ac.certified_at, ac.expires_at, ac.certifier
                FROM agent_certifications ac
                JOIN agents a ON a.id = ac.agent_id
                JOIN skills s ON s.id = ac.skill_id
                WHERE {where_sql}
                ORDER BY COALESCE(ac.expires_at, ac.certified_at + INTERVAL '999 days') ASC
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
            r["skill_name"],
            f"L{int(r['level'])}",
            r["certified_at"].date().isoformat(),
            r["expires_at"].date().isoformat() if r["expires_at"] else "—",
            r["certifier"] or "—",
        ]
        for r in rows
    ]
    title_parts = []
    if skill:
        title_parts.append(f"skill={skill}")
    if eid:
        title_parts.append(f"agent={eid}")
    if expiring:
        title_parts.append(f"expiring within {expiring}d")
    title = "Certifications — " + (", ".join(title_parts) if title_parts else "all") + f" — {len(rows)} found"
    return {
        "render": "table",
        "title": title,
        "columns": ["Agent", "ID", "Skill", "Level", "Certified", "Expires", "Certifier"],
        "rows": table_rows,
    }
