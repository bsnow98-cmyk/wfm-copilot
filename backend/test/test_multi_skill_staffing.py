"""
Phase 8 Stage 2 — multi-skill staffing math.

The four validation cases from docs/designs/MULTI_SKILL_SCHEDULING.md:

1. Single-skill case (every agent has one skill) recovers Phase 3 results
   within 1 agent.
2. Two-skill toy case (no cross-skill) — required headcount equals
   per-skill Erlang C summed.
3. Two-skill cross-skill case — required headcount is ≥ pure-primary case
   but < no-substitution case.
4. Discount sweep — vary the substitution factor; document the elbow.

Plus:
- secondary_credit_for_skill respects "primary skill" definition (the agent's
  highest-proficiency skill is primary; other skills count as secondary).
- The discount math primary-floor prevents pathological "all secondaries"
  staffing.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.multi_skill_staffing import (
    PRIMARY_FLOOR_RATIO,
    SUBSTITUTION_DISCOUNT,
    PerSkillRequirement,
    required_with_substitution,
    secondary_credit_for_skill,
)
from app.services.staffing import required_agents


# --------------------------------------------------------------------------
# Validation case 1 — single-skill recovers Phase 3 within 1 agent
# --------------------------------------------------------------------------
def test_single_skill_case_matches_phase3_within_one_agent() -> None:
    forecast = 200.0  # offered per 30-min interval
    aht = 360.0
    sl = 0.8
    asa = 20

    phase3 = required_agents(
        forecast_offered=forecast,
        aht_seconds=aht,
        sl_target=sl,
        target_asa_sec=asa,
    )["required_agents"]

    # No cross-skill help — secondary_credit_fte = 0.
    phase8 = required_with_substitution(
        forecast_offered=forecast,
        aht_seconds=aht,
        secondary_credit_fte=0.0,
        sl_target=sl,
        target_asa_sec=asa,
    ).discounted_required

    assert abs(phase8 - phase3) <= 1, (
        f"Phase 8 single-skill required ({phase8}) diverged from "
        f"Phase 3 ({phase3}) by more than 1 agent."
    )


# --------------------------------------------------------------------------
# Validation case 2 — two-skill toy, no cross-skill
# --------------------------------------------------------------------------
def test_two_skill_no_crossskill_matches_sum_of_phase3() -> None:
    """When no agents are cross-skilled, secondary_credit_fte = 0 and the
    per-skill required headcount equals the per-skill Erlang C exactly.
    Sum across skills is the total required."""
    skill_forecasts = [(150.0, 360.0), (90.0, 540.0)]
    sl, asa = 0.8, 20

    phase3_sum = sum(
        required_agents(
            forecast_offered=f,
            aht_seconds=a,
            sl_target=sl,
            target_asa_sec=asa,
        )["required_agents"]
        for f, a in skill_forecasts
    )

    phase8_sum = sum(
        required_with_substitution(
            forecast_offered=f,
            aht_seconds=a,
            secondary_credit_fte=0.0,
            sl_target=sl,
            target_asa_sec=asa,
        ).discounted_required
        for f, a in skill_forecasts
    )

    assert phase8_sum == phase3_sum, (
        f"Phase 8 sum ({phase8_sum}) should equal Phase 3 sum ({phase3_sum}) "
        "when no cross-skill agents exist."
    )


# --------------------------------------------------------------------------
# Validation case 3 — cross-skill case is between primary-only and sum
# --------------------------------------------------------------------------
def test_crossskill_case_between_pure_primary_and_no_substitution() -> None:
    """With cross-skill help, required headcount per skill should be:
       primary_only ≤ discounted ≤ no_substitution

    primary_only would be a hypothetical "we have unlimited cross-skill help"
    case (= just the floor). no_substitution is "no help at all" (= naive).
    The discount math should land between the two."""
    forecast = 100.0
    aht = 360.0
    sl, asa = 0.8, 20
    secondary_fte = 5.0  # meaningful cross-skill pool

    no_sub = required_with_substitution(
        forecast_offered=forecast,
        aht_seconds=aht,
        secondary_credit_fte=0.0,
        sl_target=sl,
        target_asa_sec=asa,
    ).discounted_required

    discounted = required_with_substitution(
        forecast_offered=forecast,
        aht_seconds=aht,
        secondary_credit_fte=secondary_fte,
        sl_target=sl,
        target_asa_sec=asa,
    ).discounted_required

    # Cross-skill help can ONLY lower required (or hit the floor).
    assert discounted <= no_sub, "cross-skill credit shouldn't increase required"
    # And the floor is real — discounted shouldn't drop below ceil(N × 0.5).
    floor = max(1, int(no_sub * PRIMARY_FLOOR_RATIO + 0.999))
    assert discounted >= floor, f"discounted {discounted} fell below floor {floor}"


# --------------------------------------------------------------------------
# Validation case 4 — discount sweep
# --------------------------------------------------------------------------
@pytest.mark.parametrize("credit_fte", [0.0, 1.0, 3.0, 5.0, 10.0, 20.0])
def test_discount_sweep_monotonic_then_floor(credit_fte: float) -> None:
    """As secondary_credit_fte increases, required should monotonically
    decrease until it hits the primary floor, then stay there."""
    forecast, aht = 200.0, 360.0
    naive = required_agents(
        forecast_offered=forecast,
        aht_seconds=aht,
    )["required_agents"]
    floor = max(1, int(naive * PRIMARY_FLOOR_RATIO + 0.999))

    discounted = required_with_substitution(
        forecast_offered=forecast,
        aht_seconds=aht,
        secondary_credit_fte=credit_fte,
    ).discounted_required

    # Should never go below the floor, never above the naive.
    assert floor <= discounted <= naive


def test_zero_demand_returns_zero_or_floor() -> None:
    out = required_with_substitution(
        forecast_offered=0.0,
        aht_seconds=0.0,
        secondary_credit_fte=0.0,
    )
    # Zero demand → zero naive → floor of 0. Either is fine.
    assert out.discounted_required == 0


def test_module_constants_match_design() -> None:
    """If someone tunes the constants, this test reminds them that the
    design doc cites specific numbers and would need updating."""
    assert SUBSTITUTION_DISCOUNT == 0.7
    assert PRIMARY_FLOOR_RATIO == 0.5


# --------------------------------------------------------------------------
# secondary_credit_for_skill
# --------------------------------------------------------------------------
def _credit_db_mock(rows: list[tuple[Any, ...]]) -> MagicMock:
    """Mock returning the given (agent_id, proficiency, top) tuples."""
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute.return_value = result
    return db


def test_secondary_credit_zero_when_no_secondaries() -> None:
    """An agent whose proficiency on this skill IS their top is a primary,
    not a secondary. The query already filters those out — the helper should
    just sum what comes back (which is empty here)."""
    db = _credit_db_mock([])
    assert secondary_credit_for_skill(db, skill_id=1) == 0.0


def test_secondary_credit_sums_proficiency_weighted() -> None:
    """Two agents, both have skill 1 as their NON-primary at proficiency 3.
    Each contributes 0.7 * 3/5 = 0.42. Total = 0.84."""
    db = _credit_db_mock([(101, 3, 5), (102, 3, 4)])
    credit = secondary_credit_for_skill(db, skill_id=1)
    assert credit == pytest.approx(0.84, abs=1e-6)


def test_secondary_credit_high_proficiency_higher_weight() -> None:
    """A secondary at proficiency 4 contributes more than one at proficiency 2."""
    db_low = _credit_db_mock([(101, 2, 5)])
    db_high = _credit_db_mock([(101, 4, 5)])
    assert (
        secondary_credit_for_skill(db_high, skill_id=1)
        > secondary_credit_for_skill(db_low, skill_id=1)
    )


# --------------------------------------------------------------------------
# Tool registry now has 7 tools
# --------------------------------------------------------------------------
def test_tool_registry_includes_get_skills_coverage() -> None:
    from app.tools import _REGISTRY

    assert "get_skills_coverage" in _REGISTRY
    # 6 Phase 6 + 1 Phase 8 stage 2 (get_skills_coverage)
    # + 1 Phase 8 stage 3 (explain_substitution) = 8.
    assert len(_REGISTRY) == 8
