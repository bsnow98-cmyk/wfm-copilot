"""
Compose per-skill demand for the multi-skill CP-SAT solver — in-memory.

The solver (`solve_multi_skill`) wants `required[(d, slot, skill_id)]`. We build
that from the per-skill MSTL forecasts (one forecast_run per skill) by running
Erlang C with the substitution discount (`required_with_substitution`) for each
interval. This is computed in-memory and NOT persisted as staffing rows — that
keeps the AGGREGATE staffing (skill_id NULL) the only staffing_requirement_intervals
set the read-side tools see, avoiding interval_start-only join fan-out.

Also provides:
- `build_aggregate_required` — the headline (d, slot) -> required curve, read from
  the aggregate staffing, used for schedule_coverage rows.
- `load_agents_with_skills` — agent_skills -> AgentWithSkills for the solver.

`d` = days since first_day; `slot` = 30-min slot index in the day (0..47).
`first_day` MUST be midnight UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.multi_skill_staffing import (
    required_with_substitution,
    secondary_credit_for_skill,
)
from app.services.scheduling_multi_skill import AgentWithSkills

INTERVAL_MIN = 30


def _d_slot(ts: datetime, first_day: datetime) -> tuple[int, int]:
    d = (ts - first_day).days
    slot = (ts.hour * 60 + ts.minute) // INTERVAL_MIN
    return d, slot


def build_required_per_skill(
    db: Session,
    *,
    per_skill_run_ids: dict[int, int],  # skill_id -> forecast_run_id
    first_day: datetime,
    horizon_days: int,
    sl_target: float = 0.80,
    target_answer_sec: int = 20,
    target_asa_sec: float = 30.0,
    shrinkage: float = 0.30,
) -> dict[tuple[int, int, int], float]:
    """Per-skill discounted required headcount, keyed (d, slot, skill_id)."""
    horizon_end = first_day + timedelta(days=horizon_days)
    required: dict[tuple[int, int, int], float] = {}

    for skill_id, run_id in per_skill_run_ids.items():
        credit = secondary_credit_for_skill(db, skill_id)
        rows = (
            db.execute(
                text(
                    """
                    SELECT interval_start, forecast_offered, forecast_aht_seconds
                    FROM forecast_intervals
                    WHERE forecast_run_id = :rid
                      AND interval_start >= :start
                      AND interval_start < :end
                    ORDER BY interval_start
                    """
                ),
                {"rid": run_id, "start": first_day, "end": horizon_end},
            )
            .mappings()
            .all()
        )
        for iv in rows:
            req = required_with_substitution(
                forecast_offered=float(iv["forecast_offered"] or 0.0),
                aht_seconds=float(iv["forecast_aht_seconds"] or 0.0),
                secondary_credit_fte=credit,
                sl_target=sl_target,
                target_answer_sec=target_answer_sec,
                target_asa_sec=target_asa_sec,
                shrinkage=shrinkage,
            )
            if req.discounted_required <= 0:
                continue
            d, slot = _d_slot(iv["interval_start"], first_day)
            required[(d, slot, skill_id)] = float(req.discounted_required)

    return required


def build_aggregate_required(
    db: Session,
    *,
    staffing_id: int,
    first_day: datetime,
    horizon_days: int,
) -> dict[tuple[int, int], int]:
    """Headline (d, slot) -> required, from the aggregate staffing intervals."""
    horizon_end = first_day + timedelta(days=horizon_days)
    rows = (
        db.execute(
            text(
                """
                SELECT interval_start, required_agents
                FROM staffing_requirement_intervals
                WHERE staffing_id = :sid
                  AND interval_start >= :start
                  AND interval_start < :end
                """
            ),
            {"sid": staffing_id, "start": first_day, "end": horizon_end},
        )
        .mappings()
        .all()
    )
    out: dict[tuple[int, int], int] = {}
    for r in rows:
        d, slot = _d_slot(r["interval_start"], first_day)
        out[(d, slot)] = int(r["required_agents"])
    return out


def load_agents_with_skills(db: Session) -> list[AgentWithSkills]:
    """Active agents with their {skill_id: proficiency} map for the solver."""
    rows = (
        db.execute(
            text(
                """
                SELECT ask.agent_id, ask.skill_id, ask.proficiency
                FROM agent_skills ask
                JOIN agents a ON a.id = ask.agent_id AND a.active = TRUE
                ORDER BY ask.agent_id, ask.skill_id
                """
            )
        )
        .mappings()
        .all()
    )
    by_agent: dict[int, dict[int, int]] = {}
    for r in rows:
        by_agent.setdefault(int(r["agent_id"]), {})[int(r["skill_id"])] = int(
            r["proficiency"]
        )
    return [
        AgentWithSkills.from_proficiency_map(agent_id, skills)
        for agent_id, skills in by_agent.items()
        if skills
    ]
