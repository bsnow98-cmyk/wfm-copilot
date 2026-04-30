"""
Phase 8 Stage 3 — multi-skill CP-SAT solver smoke tests.

These run against in-memory inputs (no DB). They check that the model
compiles, produces feasible solutions on hand-crafted toy cases, respects
skill qualification, and applies the substitution discount the way
multi_skill_staffing.py specifies.

The "real" backtest (50 agents × 7 days × 3 skills, per-skill SL ≥ 95%)
needs the seeded synthetic data from stage 1 + a Postgres connection. That
goes into the Demo Walkthrough as a manual verification step.
"""
from __future__ import annotations

import pytest

from app.services.scheduling_multi_skill import (
    AgentWithSkills,
    solve_multi_skill,
)


SALES, SUPPORT = 1, 2


def test_agent_primary_is_highest_proficiency() -> None:
    a = AgentWithSkills.from_proficiency_map(101, {SALES: 4, SUPPORT: 5})
    assert a.primary_skill_id == SUPPORT
    assert a.proficiency_factor(SUPPORT) == 1.0
    # Sales is secondary at proficiency 4 → 0.7 * 4/5 = 0.56.
    assert a.proficiency_factor(SALES) == pytest.approx(0.56, abs=1e-6)


def test_agent_with_no_skills_raises() -> None:
    with pytest.raises(ValueError):
        AgentWithSkills.from_proficiency_map(101, {})


def test_agent_unqualified_skill_returns_zero_factor() -> None:
    a = AgentWithSkills.from_proficiency_map(101, {SALES: 5})
    assert a.proficiency_factor(SUPPORT) == 0.0


def test_solver_finds_feasible_solution_for_oversupplied_demand() -> None:
    """3 agents, 1 day, 2 skills, demand fully satisfiable by primaries.

    Each agent has the matching skill; required is small enough that the
    solver should always find a feasible assignment.
    """
    agents = [
        AgentWithSkills.from_proficiency_map(1, {SALES: 5}),
        AgentWithSkills.from_proficiency_map(2, {SUPPORT: 5}),
        AgentWithSkills.from_proficiency_map(3, {SALES: 4, SUPPORT: 3}),
    ]
    required: dict[tuple[int, int, int], float] = {}
    # Demand on a single morning slot for both skills. Slot 16 = 8:00am.
    required[(0, 16, SALES)] = 1.0
    required[(0, 16, SUPPORT)] = 1.0

    out = solve_multi_skill(
        agents,
        horizon_days=1,
        required=required,
        target_shifts_per_week=1,  # only 1 day in the horizon
        max_solve_time_seconds=10,
    )
    assert out["status"] in ("optimal", "feasible")
    # Both demands should be fully covered.
    assert out["coverage"].get((0, 16, SALES), 0.0) >= 1.0 - 1e-6
    assert out["coverage"].get((0, 16, SUPPORT), 0.0) >= 1.0 - 1e-6
    # Every agent works exactly the target number of shifts (1).
    work_counts: dict[int, int] = {1: 0, 2: 0, 3: 0}
    for (agent_id, _d), pick in out["assignments"].items():
        if pick is not None:
            work_counts[agent_id] += 1
    assert work_counts == {1: 1, 2: 1, 3: 1}


def test_solver_respects_skill_qualification() -> None:
    """A sales-only agent must never be assigned to a support shift, no
    matter what the demand looks like."""
    agents = [
        AgentWithSkills.from_proficiency_map(1, {SALES: 5}),
        AgentWithSkills.from_proficiency_map(2, {SUPPORT: 5}),
    ]
    required = {(0, 16, SUPPORT): 1.0}

    out = solve_multi_skill(
        agents,
        horizon_days=1,
        required=required,
        target_shifts_per_week=1,
        max_solve_time_seconds=10,
    )
    assert out["status"] in ("optimal", "feasible")
    # Agent 1 is sales-only — if they got any shift it must be sales (but
    # we have no sales demand, so they should be off).
    assignment_for_1 = out["assignments"].get((1, 0))
    if assignment_for_1 is not None:
        _start_min, picked_skill = assignment_for_1
        assert picked_skill == SALES, (
            f"sales-only agent assigned to skill {picked_skill}"
        )


def test_solver_uses_secondary_credit_when_short_on_primaries() -> None:
    """1 sales-primary agent + 1 cross-skilled agent. Demand is for 1 sales
    FTE and 1 support FTE.

    The solver has two satisfying choices:
      a) Sales-primary works sales; cross-skilled works support (as their
         secondary, factor 0.7 * prof/5).
      b) Cross-skilled works sales; sales-primary covers nothing useful.

    Option (a) is the only one that meets both demands. The test asserts
    the solver finds it (or proves infeasibility cleanly if the demand
    can't be met given the proficiency-discounted FTE).
    """
    agents = [
        AgentWithSkills.from_proficiency_map(1, {SALES: 5}),
        AgentWithSkills.from_proficiency_map(2, {SALES: 4, SUPPORT: 4}),
    ]
    # Cross-skilled agent has SALES at 4 (primary) and SUPPORT at 4 (secondary
    # via factor 0.7*4/5 = 0.56). So support coverage from the secondary is
    # capped at 0.56 FTE per shift.
    required = {(0, 16, SALES): 1.0, (0, 16, SUPPORT): 0.5}
    out = solve_multi_skill(
        agents,
        horizon_days=1,
        required=required,
        target_shifts_per_week=1,
        max_solve_time_seconds=10,
    )
    assert out["status"] in ("optimal", "feasible")
    # Both demands covered (allowing for floating-point tolerance).
    assert out["coverage"].get((0, 16, SALES), 0.0) >= 1.0 - 1e-6
    assert out["coverage"].get((0, 16, SUPPORT), 0.0) >= 0.5 - 1e-6


def test_solver_understaffed_count_is_accurate() -> None:
    """If demand exceeds supply, total_understaffed_intervals should be > 0
    and coverage should fall short."""
    agents = [
        AgentWithSkills.from_proficiency_map(1, {SALES: 5}),
    ]
    # Demand 5 sales FTE, only 1 agent. Slot 16 is the only one that can be
    # covered (and only by 1 FTE).
    required = {
        (0, 16, SALES): 5.0,
        (0, 17, SALES): 5.0,
    }
    out = solve_multi_skill(
        agents,
        horizon_days=1,
        required=required,
        target_shifts_per_week=1,
        max_solve_time_seconds=10,
    )
    assert out["status"] in ("optimal", "feasible")
    assert out["total_understaffed_intervals"] >= 1


def test_solver_infeasible_on_zero_qualified_supply() -> None:
    """Demand for a skill no agent has — impossible to cover, but the model
    should still produce a solution (with shortfall) rather than hang."""
    agents = [
        AgentWithSkills.from_proficiency_map(1, {SALES: 5}),
    ]
    required = {(0, 16, SUPPORT): 1.0}
    out = solve_multi_skill(
        agents,
        horizon_days=1,
        required=required,
        target_shifts_per_week=1,
        max_solve_time_seconds=10,
    )
    # The solver finds a "feasible" answer even with the unmet demand —
    # shortfall is in the objective, not a hard constraint.
    assert out["status"] in ("optimal", "feasible")
    assert out["total_understaffed_intervals"] >= 1
    # Coverage on the support demand is exactly 0.
    assert out["coverage"].get((0, 16, SUPPORT), 0.0) == pytest.approx(0.0)
