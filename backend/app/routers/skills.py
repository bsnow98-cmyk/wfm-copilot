"""
/skills router — read-only skill catalogue.

Phase 8 stage 5+ — frontend's first real-data bridge. The dashboard's skill
picker has been hardcoded to sales/support/billing; this endpoint lets it
fetch the live list. Backwards-compatible: if the frontend can't reach the
API (no `NEXT_PUBLIC_API_URL` set), it falls back to the hardcoded list.

Returns one row per skill with active-agent counts split by primary vs
secondary. The counts make this useful for the dashboard's
SkillBadgeRow once we wire it up to live data.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db

log = logging.getLogger("wfm.skills")
router = APIRouter(prefix="/skills", tags=["skills"])


class SkillOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    primary_agent_count: int
    secondary_agent_count: int


@router.get("", response_model=list[SkillOut])
def list_skills(db: Session = Depends(get_db)) -> list[SkillOut]:
    """List all skills with primary/secondary agent counts.

    Primary = agent's highest-proficiency skill (may be tied — the row counts
    each tie-skill once, which gives a slightly inflated count if anyone has
    multiple skills at proficiency 5; acceptable in v1).
    Secondary = any other skill the agent has > 0 proficiency on.
    """
    rows = (
        db.execute(
            text(
                """
                WITH max_prof AS (
                    SELECT agent_id, MAX(proficiency) AS top
                    FROM agent_skills GROUP BY agent_id
                ),
                primaries AS (
                    SELECT a_skill.skill_id, COUNT(*) AS cnt
                    FROM agent_skills a_skill
                    JOIN max_prof mp ON mp.agent_id = a_skill.agent_id
                    JOIN agents a    ON a.id = a_skill.agent_id AND a.active = TRUE
                    WHERE a_skill.proficiency = mp.top
                    GROUP BY a_skill.skill_id
                ),
                secondaries AS (
                    SELECT a_skill.skill_id, COUNT(*) AS cnt
                    FROM agent_skills a_skill
                    JOIN max_prof mp ON mp.agent_id = a_skill.agent_id
                    JOIN agents a    ON a.id = a_skill.agent_id AND a.active = TRUE
                    WHERE a_skill.proficiency < mp.top
                    GROUP BY a_skill.skill_id
                )
                SELECT s.id, s.name, s.description,
                       COALESCE(p.cnt, 0) AS primary_count,
                       COALESCE(sec.cnt, 0) AS secondary_count
                FROM skills s
                LEFT JOIN primaries  p    ON p.skill_id = s.id
                LEFT JOIN secondaries sec ON sec.skill_id = s.id
                ORDER BY s.name
                """
            )
        )
        .mappings()
        .all()
    )
    return [
        SkillOut(
            id=int(r["id"]),
            name=r["name"],
            description=r["description"],
            primary_agent_count=int(r["primary_count"]),
            secondary_agent_count=int(r["secondary_count"]),
        )
        for r in rows
    ]
