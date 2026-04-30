"""
Phase 8 Stage 3 — multi-skill CP-SAT scheduling.

This module is a parallel solver to `app/services/scheduling.py`. The Phase 4
single-skill model stays intact; multi-skill is opt-in. The two solvers
diverge on a single decision variable: the multi-skill model adds a *skill*
axis to the per-(agent, day) shift assignment.

DESIGN
------
v1 follows the "agents stay on one skill per shift" deferral from the design
doc. So the variables are:

  assign[a, d, s_idx, k] ∈ {0, 1}      — agent `a` starts at shift index s_idx
                                         on day `d` working skill `k`.
  off[a, d]              ∈ {0, 1}      — agent is off on day `d`.

Each (a, d) row picks exactly ONE of: an (s_idx, k) pair the agent is qualified
for, or `off`. Skills are agent-specific — the variable is only created for
(a, k) combinations where the agent has proficiency on `k`. That keeps the
variable count linear in (avg skills per agent), not (n_agents × n_skills).

Coverage constraint: for each (day, slot, skill), the sum of `assign[a, d,
s_idx, k] × proficiency_factor[a][k]` for shifts that cover the slot must
≥ required[d, slot, k]. proficiency_factor is 1.0 for primaries, the
multi-skill discount for secondaries (matching `multi_skill_staffing.py`).

Hard constraints H2/H3/H4 (shifts per week / min rest / max consecutive
days) carry over from Phase 4 unchanged.

Objective: minimize per-skill shortage + small overage penalty. Same shape
as Phase 4, just now indexed by skill too.

WHAT'S NOT HERE
---------------
- DB I/O. The Phase 4 ScheduleService persists schedules + shift_segments +
  schedule_coverage. A multi-skill version of that would be a service-layer
  wrapper over `solve_multi_skill`. Stage 3 ships the math; the persistence
  + chat-tool integration is stage 4 territory.
- The "anti-thrashing" constraint from the design doc is moot under the
  one-skill-per-shift model (the design carried a small inconsistency
  between the deferral and the constraint description; this module follows
  the deferral). Per-interval skill switching with K=2 anti-thrashing is the
  v1.1 path.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TypedDict

from ortools.sat.python import cp_model

from app.services.multi_skill_staffing import (
    PROFICIENCY_DENOMINATOR,
    SUBSTITUTION_DISCOUNT,
)

log = logging.getLogger("wfm.scheduling.multi_skill")

# --- defaults -- mirror scheduling.py so the two stay structurally close ---
SHIFT_LENGTH_MIN = 480
INTERVAL_MIN = 30
INTERVALS_PER_DAY = 24 * 60 // INTERVAL_MIN  # 48
SHIFT_START_FIRST_MIN = 6 * 60
SHIFT_START_LAST_MIN = 12 * 60
SHIFT_START_STEP_MIN = 30
TARGET_SHIFTS_PER_WEEK = 5
MIN_REST_HOURS = 11
MAX_CONSECUTIVE_DAYS = 6
DEFAULT_SOLVE_TIME_S = 90  # ~3x Phase 4's 30s — design doc accepted up to 90s
OVERSTAFF_PENALTY_PCT = 10
PRIMARY_PROFICIENCY_THRESHOLD = 3  # agents below this on a skill are "secondary only"


def _candidate_starts() -> list[int]:
    return list(range(SHIFT_START_FIRST_MIN, SHIFT_START_LAST_MIN + 1, SHIFT_START_STEP_MIN))


def _shift_covers_interval(start_min: int, slot_min: int) -> bool:
    return start_min <= slot_min < start_min + SHIFT_LENGTH_MIN


# --- inputs ---------------------------------------------------------------
@dataclass(frozen=True)
class AgentWithSkills:
    """One agent's qualifications. `skills` maps skill_id → proficiency (1-5).

    `primary_skill_id` is the skill_id with the highest proficiency. Ties
    broken by lowest skill_id for determinism.
    """

    id: int
    skills: dict[int, int]
    primary_skill_id: int

    @classmethod
    def from_proficiency_map(cls, agent_id: int, skills: dict[int, int]) -> "AgentWithSkills":
        if not skills:
            raise ValueError(f"agent {agent_id} has no skills")
        primary = min(
            skills.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[0]
        return cls(id=agent_id, skills=dict(skills), primary_skill_id=primary)

    def proficiency_factor(self, skill_id: int) -> float:
        """1.0 for the primary skill; SUBSTITUTION_DISCOUNT × prof / 5 for secondaries.

        Returns 0.0 if the agent has no proficiency on the skill — used
        by the model to gate the variable.
        """
        prof = self.skills.get(skill_id, 0)
        if prof <= 0:
            return 0.0
        if skill_id == self.primary_skill_id:
            return 1.0
        return SUBSTITUTION_DISCOUNT * prof / PROFICIENCY_DENOMINATOR


# --- result ---------------------------------------------------------------
class MultiSkillResult(TypedDict):
    status: str  # 'optimal' | 'feasible' | 'infeasible' | 'failed'
    runtime_seconds: float
    objective_value: float
    total_understaffed_intervals: int
    # (agent_id, day) → (start_min, skill_id) or None
    assignments: dict[tuple[int, int], tuple[int, int] | None]
    # (day, slot, skill_id) → effective FTE coverage (float because secondary
    # contributions are fractional)
    coverage: dict[tuple[int, int, int], float]


# --- the solver -----------------------------------------------------------
def solve_multi_skill(
    agents: list[AgentWithSkills],
    horizon_days: int,
    required: dict[tuple[int, int, int], float],
    *,
    target_shifts_per_week: int = TARGET_SHIFTS_PER_WEEK,
    min_rest_hours: int = MIN_REST_HOURS,
    max_consecutive_days: int = MAX_CONSECUTIVE_DAYS,
    max_solve_time_seconds: int = DEFAULT_SOLVE_TIME_S,
    num_search_workers: int = 8,  # design doc — multi-skill needs more parallelism
) -> MultiSkillResult:
    """Build + solve the multi-skill CP-SAT model. Pure: no DB.

    `required[d, slot, k]` is per-skill demand. Coverage uses effective-FTE
    accounting — primaries count 1.0, secondaries count their discount.
    Coverage values are scaled by 100 internally so CP-SAT (integer-only)
    can handle the discount fractions. Required is also scaled by 100 in
    the constraint.
    """
    starts = _candidate_starts()
    num_starts = len(starts)
    n_agents = len(agents)
    skills_set = sorted({k for d, slot, k in required.keys()})

    model = cp_model.CpModel()

    # Variables: assign[a, d, s_idx, k] for skills the agent is qualified on.
    # Plus off[a, d] = 1 if agent is off on day d.
    assign: dict[tuple[int, int, int, int], cp_model.IntVar] = {}
    off: dict[tuple[int, int], cp_model.IntVar] = {}
    for ai, agent in enumerate(agents):
        for d in range(horizon_days):
            off[ai, d] = model.NewBoolVar(f"off_a{ai}_d{d}")
            for s_idx in range(num_starts):
                for k in agent.skills:
                    assign[ai, d, s_idx, k] = model.NewBoolVar(
                        f"assign_a{ai}_d{d}_s{s_idx}_k{k}"
                    )

    # H1: exactly one of {assign(a,d,*,*), off(a,d)} is 1.
    for ai, agent in enumerate(agents):
        for d in range(horizon_days):
            options = [off[ai, d]]
            for s_idx in range(num_starts):
                for k in agent.skills:
                    options.append(assign[ai, d, s_idx, k])
            model.AddExactlyOne(options)

    # H2: each agent works exactly target_shifts_per_week days.
    for ai, agent in enumerate(agents):
        working_vars = [
            assign[ai, d, s_idx, k]
            for d in range(horizon_days)
            for s_idx in range(num_starts)
            for k in agent.skills
        ]
        model.Add(sum(working_vars) == target_shifts_per_week)

    # H3: min rest hours between consecutive-day shifts. Operates over
    # shift starts regardless of skill (skill choice doesn't affect end time).
    rest_min = min_rest_hours * 60
    for ai, agent in enumerate(agents):
        for d in range(horizon_days - 1):
            for s_today_idx, s_today in enumerate(starts):
                end_today = s_today + SHIFT_LENGTH_MIN
                for s_tom_idx, s_tomorrow in enumerate(starts):
                    gap = (s_tomorrow + 1440) - end_today
                    if gap < rest_min:
                        # Forbid this shift pair regardless of skill choice.
                        for k_today in agent.skills:
                            for k_tom in agent.skills:
                                model.Add(
                                    assign[ai, d, s_today_idx, k_today]
                                    + assign[ai, d + 1, s_tom_idx, k_tom]
                                    <= 1
                                )

    # H4: max consecutive working days.
    if max_consecutive_days < horizon_days:
        window = max_consecutive_days + 1
        for ai, agent in enumerate(agents):
            for start_d in range(horizon_days - window + 1):
                worked = []
                for d in range(start_d, start_d + window):
                    for s_idx in range(num_starts):
                        for k in agent.skills:
                            worked.append(assign[ai, d, s_idx, k])
                model.Add(sum(worked) <= max_consecutive_days)

    # Coverage: for each (day, slot, skill), effective FTE ≥ required.
    # Internally we scale by 100 so we can keep integer arithmetic in CP-SAT.
    SCALE = 100
    shift_covers_slot = {}
    for s_idx, s_min in enumerate(starts):
        for slot in range(INTERVALS_PER_DAY):
            shift_covers_slot[s_idx, slot] = _shift_covers_interval(s_min, slot * INTERVAL_MIN)

    shortage_vars: list[cp_model.IntVar] = []
    overage_vars: list[cp_model.IntVar] = []

    for d in range(horizon_days):
        for slot in range(INTERVALS_PER_DAY):
            for k in skills_set:
                req_float = required.get((d, slot, k), 0.0)
                req_scaled = int(round(req_float * SCALE))
                # Sum scaled effective coverage from any agent assigned to this
                # slot working this skill.
                cov_terms = []
                max_possible = 0
                for ai, agent in enumerate(agents):
                    if k not in agent.skills:
                        continue
                    factor = agent.proficiency_factor(k)
                    factor_scaled = int(round(factor * SCALE))
                    if factor_scaled <= 0:
                        continue
                    for s_idx in range(num_starts):
                        if not shift_covers_slot[s_idx, slot]:
                            continue
                        cov_terms.append(factor_scaled * assign[ai, d, s_idx, k])
                        max_possible += factor_scaled
                if not cov_terms and req_scaled == 0:
                    continue
                cov_expr = sum(cov_terms) if cov_terms else 0

                if req_scaled > 0:
                    short = model.NewIntVar(0, req_scaled, f"short_d{d}_t{slot}_k{k}")
                    model.Add(short >= req_scaled - cov_expr)
                    shortage_vars.append(short)
                if max_possible > 0:
                    over = model.NewIntVar(0, max_possible, f"over_d{d}_t{slot}_k{k}")
                    model.Add(over >= cov_expr - max(req_scaled, 0))
                    overage_vars.append(over)

    total_short = sum(shortage_vars) if shortage_vars else 0
    total_over = sum(overage_vars) if overage_vars else 0
    model.Minimize(100 * total_short + OVERSTAFF_PENALTY_PCT * total_over)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_solve_time_seconds
    solver.parameters.num_search_workers = num_search_workers
    solver.parameters.linearization_level = 1  # design doc tuning hint

    t0 = _perf_now()
    status = solver.Solve(model)
    runtime = _perf_now() - t0

    status_label = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "failed",
        cp_model.MODEL_INVALID: "failed",
    }.get(status, "failed")

    if status_label in ("infeasible", "failed"):
        return MultiSkillResult(
            status=status_label,
            runtime_seconds=runtime,
            objective_value=0.0,
            total_understaffed_intervals=0,
            assignments={},
            coverage={},
        )

    # Extract assignments.
    assignments: dict[tuple[int, int], tuple[int, int] | None] = {}
    for ai, agent in enumerate(agents):
        for d in range(horizon_days):
            if solver.Value(off[ai, d]) == 1:
                assignments[agent.id, d] = None
                continue
            picked = None
            for s_idx, s_min in enumerate(starts):
                for k in agent.skills:
                    if solver.Value(assign[ai, d, s_idx, k]) == 1:
                        picked = (s_min, k)
                        break
                if picked is not None:
                    break
            assignments[agent.id, d] = picked

    # Realized coverage (effective FTE, unscaled).
    coverage: dict[tuple[int, int, int], float] = {}
    for d in range(horizon_days):
        for slot in range(INTERVALS_PER_DAY):
            for k in skills_set:
                cov = 0.0
                slot_min = slot * INTERVAL_MIN
                for ai, agent in enumerate(agents):
                    if k not in agent.skills:
                        continue
                    factor = agent.proficiency_factor(k)
                    if factor <= 0.0:
                        continue
                    for s_idx, s_min in enumerate(starts):
                        if not _shift_covers_interval(s_min, slot_min):
                            continue
                        if solver.Value(assign[ai, d, s_idx, k]) == 1:
                            cov += factor
                if cov > 0 or required.get((d, slot, k), 0.0) > 0:
                    coverage[d, slot, k] = round(cov, 2)

    understaffed = sum(
        1
        for (d, slot, k), req in required.items()
        if req > 0 and coverage.get((d, slot, k), 0.0) < req - 1e-6
    )

    return MultiSkillResult(
        status=status_label,
        runtime_seconds=runtime,
        objective_value=float(solver.ObjectiveValue()),
        total_understaffed_intervals=understaffed,
        assignments=assignments,
        coverage=coverage,
    )


def _perf_now() -> float:
    import time
    return time.perf_counter()
