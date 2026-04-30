"""
Phase 8 Stage 2 — per-skill required-agents math with substitution discount.

Honest about what this is and isn't (design doc, "The math caveat"): the
correct multi-skill staffing math needs Monte Carlo simulation. This is a
discount-based approximation that's documented, inspectable, and good
enough for a portfolio-grade demo. If real-ops deployment ever happens,
swap this for a calibrated simulator.

Inputs and outputs are pure (no DB). The DB layer lives in
`app/services/staffing.py` (Phase 3) and the new `secondary_credit_for_skill`
helper below — composing those into a per-skill staffing service is the
caller's job.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.staffing import required_agents

# Tunables — exposed as module constants so the chat tool that explains the
# math (`explain_substitution`, Stage 5) can cite them.
SUBSTITUTION_DISCOUNT = 0.7    # secondary skills count as this fraction of an FTE
PRIMARY_FLOOR_RATIO = 0.5      # at least this fraction of N must be primaries
PROFICIENCY_DENOMINATOR = 5    # proficiency is on a 1-5 scale


@dataclass(frozen=True)
class PerSkillRequirement:
    """One interval's required headcount with the discount applied."""

    skill_id: int | None
    forecast_offered: float
    expected_aht_sec: float
    naive_required: int
    secondary_credit_fte: float
    discounted_required: int

    @property
    def primary_floor(self) -> int:
        return math.ceil(self.naive_required * PRIMARY_FLOOR_RATIO)


def required_with_substitution(
    *,
    forecast_offered: float,
    aht_seconds: float,
    secondary_credit_fte: float,
    sl_target: float | None = 0.80,
    target_answer_sec: int = 20,
    target_asa_sec: float | None = 30.0,
    shrinkage: float = 0.30,
    primary_floor_ratio: float = PRIMARY_FLOOR_RATIO,
) -> PerSkillRequirement:
    """Required agents for a single skill in a single interval.

    Steps (matches design doc):
      1. N_s = single-skill Erlang C against the forecast.
      2. Subtract `secondary_credit_fte` (cross-skill help estimate).
      3. Floor at ceil(N_s × primary_floor_ratio).

    `secondary_credit_fte` is computed elsewhere (`secondary_credit_for_skill`)
    from the agent roster — it's a population statistic, not per-interval.
    Floored at 0 so a wildly inflated credit can't drive required negative.
    """
    naive = required_agents(
        forecast_offered=forecast_offered,
        aht_seconds=aht_seconds,
        sl_target=sl_target,
        target_answer_sec=target_answer_sec,
        target_asa_sec=target_asa_sec,
        shrinkage=shrinkage,
    )["required_agents"]

    after_credit = max(0, naive - math.ceil(secondary_credit_fte))
    floor_n = math.ceil(naive * primary_floor_ratio)
    discounted = max(after_credit, floor_n)

    return PerSkillRequirement(
        skill_id=None,
        forecast_offered=forecast_offered,
        expected_aht_sec=aht_seconds,
        naive_required=naive,
        secondary_credit_fte=secondary_credit_fte,
        discounted_required=discounted,
    )


def secondary_credit_for_skill(
    db: Session,
    skill_id: int,
    *,
    discount: float = SUBSTITUTION_DISCOUNT,
    proficiency_denominator: int = PROFICIENCY_DENOMINATOR,
) -> float:
    """How many FTEs of secondary-skill help is available for this skill.

    For each agent who has `skill_id` as a NON-PRIMARY skill (their primary is
    something they're more proficient at), contribute `discount * proficiency
    / proficiency_denominator` FTE. Agents with `skill_id` as their highest
    proficiency are PRIMARIES on that skill — they're counted in the naive
    Erlang C and shouldn't double-count here.

    This is a population statistic — it doesn't change per interval. The
    interpretation: "on average, this is the cross-skill help we expect to
    flow into this skill when needed." A truly correct multi-skill staffing
    model would simulate the routing decisions; we approximate.
    """
    rows = db.execute(
        text(
            """
            WITH max_prof AS (
                SELECT agent_id, MAX(proficiency) AS top
                FROM agent_skills
                GROUP BY agent_id
            )
            SELECT a_skill.agent_id, a_skill.proficiency, mp.top
            FROM agent_skills a_skill
            JOIN max_prof mp ON mp.agent_id = a_skill.agent_id
            JOIN agents ag   ON ag.id = a_skill.agent_id AND ag.active = TRUE
            WHERE a_skill.skill_id = :skill_id
              AND a_skill.proficiency < mp.top
            """
        ),
        {"skill_id": skill_id},
    ).all()

    credit = 0.0
    for row in rows:
        prof = int(row[1])
        credit += discount * prof / proficiency_denominator
    return credit
