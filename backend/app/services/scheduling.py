"""
ScheduleService — OR-Tools CP-SAT model that assigns agents to shifts to cover
a per-interval staffing requirement.

MODEL OVERVIEW
--------------
Decision: for each (agent, day), choose ONE of:
    - "off"  (no shift that day), OR
    - one of the candidate shift start times (e.g. 6:00, 6:30, ..., 12:00)
Each shift is a fixed length (default 8 hours). Coverage of an interval is
just the count of agents whose chosen shift covers that interval.

Hard constraints:
    H1. Exactly one assignment per (agent, day) — including "off"
    H2. Each agent works exactly `target_shifts_per_week` days (default 5)
    H3. Min `min_rest_hours` between consecutive shifts (default 11)
    H4. Max `max_consecutive_days` worked in a row (default 6)

Objective:
    Minimize total under-staffing across all intervals (in agent-intervals),
    plus a small penalty for over-staffing to avoid wasted capacity.

WHY CP-SAT?
-----------
Boolean shift assignments + linear coverage constraints + linear objective is
the textbook fit for CP-SAT. It handles 50 agents × 7 days × 13 start options
(≈4,500 booleans) in seconds. For 200+ agents you'd use the same model but
budget more solve time and tune `num_search_workers`.

REFERENCES
----------
- Google OR-Tools shift scheduling examples:
    https://github.com/google/or-tools/blob/master/examples/python/shift_scheduling_sat.py
- Cleveland, "Call Center Management on Fast Forward" — chapters on shift
  pattern design.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from ortools.sat.python import cp_model
from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("wfm.scheduling")


# --- defaults ---------------------------------------------------------
SHIFT_LENGTH_MIN = 480              # 8 hours
INTERVAL_MIN = 30                   # planning cadence
INTERVALS_PER_DAY = 24 * 60 // INTERVAL_MIN   # 48
SHIFT_START_FIRST_MIN = 6 * 60      # earliest start = 6:00
SHIFT_START_LAST_MIN = 12 * 60      # latest start = 12:00 (would end at 20:00)
SHIFT_START_STEP_MIN = 30           # every 30 min
TARGET_SHIFTS_PER_WEEK = 5
MIN_REST_HOURS = 11
MAX_CONSECUTIVE_DAYS = 6
DEFAULT_SOLVE_TIME_S = 60
OVERSTAFF_PENALTY_PCT = 10          # objective weight: 1 unit understaff = 10 units overstaff
                                    # (so under-staffing dominates by 10x)


def _candidate_starts() -> list[int]:
    """List of shift start times in minutes-from-midnight."""
    return list(range(SHIFT_START_FIRST_MIN, SHIFT_START_LAST_MIN + 1, SHIFT_START_STEP_MIN))


def _shift_covers_interval(start_min: int, interval_min: int) -> bool:
    """Does the 8-hr shift starting at start_min cover the 30-min interval
    starting at interval_min?"""
    return start_min <= interval_min < start_min + SHIFT_LENGTH_MIN


# --- result types -----------------------------------------------------
class SolverSummary(TypedDict):
    schedule_id: int
    solver_status: str           # 'optimal' | 'feasible' | 'infeasible' | 'failed'
    runtime_seconds: float
    objective_value: float
    total_understaffed_intervals: int
    shift_segments_written: int
    coverage_rows_written: int


# --- the service ------------------------------------------------------
class ScheduleService:
    def __init__(self, db: Session):
        self.db = db

    # ----- public entry point ---------------------------------------
    def solve(
        self,
        staffing_id: int,
        name: str | None = None,
        agent_count: int | None = None,
        horizon_days: int = 7,
        target_shifts_per_week: int = TARGET_SHIFTS_PER_WEEK,
        min_rest_hours: int = MIN_REST_HOURS,
        max_consecutive_days: int = MAX_CONSECUTIVE_DAYS,
        max_solve_time_seconds: int = DEFAULT_SOLVE_TIME_S,
    ) -> SolverSummary:
        """Run the CP-SAT model. Persists schedule + shift_segments + coverage.

        Returns SolverSummary describing how it went.
        """
        t_start = time.time()

        # 1. Load staffing requirement intervals (the demand to cover).
        staffing = self.db.execute(
            text("SELECT id, forecast_run_id FROM staffing_requirements WHERE id=:id"),
            {"id": staffing_id},
        ).mappings().first()
        if not staffing:
            raise ValueError(f"staffing_requirements id={staffing_id} not found")

        all_intervals = self.db.execute(
            text("""
                SELECT interval_start, required_agents
                FROM staffing_requirement_intervals
                WHERE staffing_id = :id
                ORDER BY interval_start
            """),
            {"id": staffing_id},
        ).mappings().all()
        if not all_intervals:
            raise ValueError(f"No intervals on staffing {staffing_id}")

        # 2. Load agents (limit to first N if requested).
        agent_query = "SELECT id, employee_id, full_name FROM agents WHERE active=TRUE ORDER BY id"
        if agent_count:
            agent_query += f" LIMIT {int(agent_count)}"
        agents = self.db.execute(text(agent_query)).mappings().all()
        if not agents:
            raise ValueError(
                "No active agents in DB. Run `python -m scripts.seed_agents` first."
            )
        log.info("Solving for %d agents over %d days", len(agents), horizon_days)

        # 3. Slice the staffing intervals to our horizon (start at earliest interval).
        first_ts = all_intervals[0]["interval_start"]
        # Snap to midnight UTC of that day (we model day-granular shifts).
        first_day = first_ts.replace(hour=0, minute=0, second=0, microsecond=0)
        horizon_end = first_day + timedelta(days=horizon_days)
        horizon_intervals = [
            iv for iv in all_intervals
            if first_day <= iv["interval_start"] < horizon_end
        ]
        if len(horizon_intervals) < INTERVALS_PER_DAY * horizon_days * 0.5:
            log.warning(
                "Staffing covers only %d intervals in horizon (expected ~%d). "
                "Forecast may be shorter than horizon_days.",
                len(horizon_intervals), INTERVALS_PER_DAY * horizon_days,
            )

        # Build a (day_idx, slot_idx) -> required map.
        required: dict[tuple[int, int], int] = {}
        for iv in horizon_intervals:
            ts = iv["interval_start"]
            day_idx = (ts - first_day).days
            slot_idx = (ts.hour * 60 + ts.minute) // INTERVAL_MIN
            required[(day_idx, slot_idx)] = int(iv["required_agents"])
        # Closed-hour intervals not in the staffing data default to 0 demand.

        # 4. Insert the schedule row in 'running' state.
        schedule_id = self._create_schedule_row(
            staffing_id=staffing_id,
            name=name or f"Auto-scheduled (staffing={staffing_id})",
            start_date=first_day.date(),
            horizon_days=horizon_days,
        )

        # 5. Build and solve the CP-SAT model.
        try:
            model_result = self._build_and_solve(
                agents=agents,
                horizon_days=horizon_days,
                required=required,
                target_shifts_per_week=target_shifts_per_week,
                min_rest_hours=min_rest_hours,
                max_consecutive_days=max_consecutive_days,
                max_solve_time_seconds=max_solve_time_seconds,
            )
        except Exception as exc:
            self._mark_failed(schedule_id, str(exc), time.time() - t_start)
            log.exception("Solve failed")
            raise

        # 6. Persist results.
        segments_written = self._write_shift_segments(
            schedule_id=schedule_id,
            agents=agents,
            assignments=model_result["assignments"],
            first_day=first_day,
        )
        coverage_written = self._write_coverage(
            schedule_id=schedule_id,
            horizon_days=horizon_days,
            first_day=first_day,
            required=required,
            coverage=model_result["coverage"],
        )
        runtime = time.time() - t_start
        self._mark_completed(
            schedule_id=schedule_id,
            solver_status=model_result["status_label"],
            runtime_seconds=runtime,
            objective_value=model_result["objective_value"],
            total_understaffed_intervals=model_result["total_understaffed"],
        )

        log.info(
            "Schedule %s solved (%s) in %.2fs — objective=%.0f, understaffed_intervals=%d",
            schedule_id, model_result["status_label"], runtime,
            model_result["objective_value"], model_result["total_understaffed"],
        )

        return SolverSummary(
            schedule_id=schedule_id,
            solver_status=model_result["status_label"],
            runtime_seconds=runtime,
            objective_value=model_result["objective_value"],
            total_understaffed_intervals=model_result["total_understaffed"],
            shift_segments_written=segments_written,
            coverage_rows_written=coverage_written,
        )

    # ----- the actual CP-SAT model -----------------------------------
    def _build_and_solve(
        self,
        agents: list,
        horizon_days: int,
        required: dict[tuple[int, int], int],
        target_shifts_per_week: int,
        min_rest_hours: int,
        max_consecutive_days: int,
        max_solve_time_seconds: int,
    ) -> dict:
        starts = _candidate_starts()
        num_starts = len(starts)
        n_agents = len(agents)

        model = cp_model.CpModel()

        # x[a, d, s] = 1 if agent a starts a shift at start-time index s on day d.
        # Index s == num_starts represents "off" (no shift).
        OFF = num_starts
        x = {}
        for a in range(n_agents):
            for d in range(horizon_days):
                for s in range(num_starts + 1):
                    x[a, d, s] = model.NewBoolVar(f"x_a{a}_d{d}_s{s}")

        # H1: exactly one assignment per (agent, day) — including "off".
        for a in range(n_agents):
            for d in range(horizon_days):
                model.AddExactlyOne([x[a, d, s] for s in range(num_starts + 1)])

        # H2: agent works exactly target_shifts_per_week days.
        for a in range(n_agents):
            working = [x[a, d, s] for d in range(horizon_days) for s in range(num_starts)]
            model.Add(sum(working) == target_shifts_per_week)

        # H3: min rest between consecutive-day shifts.
        # For each agent and adjacent day pair, forbid shift pairs with insufficient gap.
        rest_min = min_rest_hours * 60
        for a in range(n_agents):
            for d in range(horizon_days - 1):
                for s_today_idx, s_today in enumerate(starts):
                    end_today = s_today + SHIFT_LENGTH_MIN     # minutes from day-d midnight
                    for s_tom_idx, s_tomorrow in enumerate(starts):
                        # gap = (start_tomorrow + 1440) - end_today
                        gap = (s_tomorrow + 1440) - end_today
                        if gap < rest_min:
                            # These two shifts are mutually exclusive on consecutive days.
                            model.Add(x[a, d, s_today_idx] + x[a, d + 1, s_tom_idx] <= 1)

        # H4: max consecutive working days. Sliding window.
        if max_consecutive_days < horizon_days:
            window = max_consecutive_days + 1
            for a in range(n_agents):
                for start_d in range(horizon_days - window + 1):
                    working_in_window = []
                    for d in range(start_d, start_d + window):
                        working_in_window.extend(x[a, d, s] for s in range(num_starts))
                    model.Add(sum(working_in_window) <= max_consecutive_days)

        # Coverage per interval: how many agents are working at slot t on day d?
        # Pre-compute: for each (start_idx, slot), does this shift cover this slot?
        shift_covers_slot: dict[tuple[int, int], bool] = {}
        for s_idx, s_min in enumerate(starts):
            for slot in range(INTERVALS_PER_DAY):
                slot_min = slot * INTERVAL_MIN
                shift_covers_slot[s_idx, slot] = _shift_covers_interval(s_min, slot_min)

        # shortage[d, slot] = max(0, required - coverage). We only care about slots
        # with positive demand.
        shortage_vars = []
        overage_vars = []
        coverage_expressions: dict[tuple[int, int], cp_model.LinearExprT] = {}
        max_possible_coverage = n_agents

        for d in range(horizon_days):
            for slot in range(INTERVALS_PER_DAY):
                req = required.get((d, slot), 0)
                cov_terms = [
                    x[a, d, s_idx]
                    for a in range(n_agents)
                    for s_idx in range(num_starts)
                    if shift_covers_slot[s_idx, slot]
                ]
                if not cov_terms:
                    continue
                cov_expr = sum(cov_terms)
                coverage_expressions[d, slot] = cov_expr

                if req > 0:
                    short = model.NewIntVar(0, req, f"short_d{d}_t{slot}")
                    model.Add(short >= req - cov_expr)
                    shortage_vars.append(short)
                # Over-staffing penalty (kept smaller than under-staffing).
                over = model.NewIntVar(0, max_possible_coverage, f"over_d{d}_t{slot}")
                model.Add(over >= cov_expr - max(req, 0))
                overage_vars.append(over)

        # Objective: heavily penalize shortage, lightly penalize overage.
        total_short = sum(shortage_vars) if shortage_vars else 0
        total_over = sum(overage_vars) if overage_vars else 0
        model.Minimize(100 * total_short + OVERSTAFF_PENALTY_PCT * total_over)

        # Solve.
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max_solve_time_seconds
        solver.parameters.num_search_workers = 4    # parallelism on multi-core
        status = solver.Solve(model)
        status_label = {
            cp_model.OPTIMAL: "optimal",
            cp_model.FEASIBLE: "feasible",
            cp_model.INFEASIBLE: "infeasible",
            cp_model.UNKNOWN: "failed",
            cp_model.MODEL_INVALID: "failed",
        }.get(status, "failed")

        if status_label in ("infeasible", "failed"):
            return {
                "status_label": status_label,
                "objective_value": 0.0,
                "total_understaffed": 0,
                "assignments": {},
                "coverage": {},
            }

        # Extract assignments: (agent_index, day) -> start_minute or None
        assignments: dict[tuple[int, int], int | None] = {}
        for a in range(n_agents):
            for d in range(horizon_days):
                if solver.Value(x[a, d, OFF]) == 1:
                    assignments[a, d] = None
                    continue
                for s_idx, s_min in enumerate(starts):
                    if solver.Value(x[a, d, s_idx]) == 1:
                        assignments[a, d] = s_min
                        break

        # Realized coverage from assignments — for the cache table.
        coverage: dict[tuple[int, int], int] = {}
        for d in range(horizon_days):
            for slot in range(INTERVALS_PER_DAY):
                if (d, slot) not in coverage_expressions and required.get((d, slot), 0) == 0:
                    coverage[d, slot] = 0
                    continue
                count = 0
                slot_min = slot * INTERVAL_MIN
                for (a, dd), s_min in assignments.items():
                    if dd != d or s_min is None:
                        continue
                    if _shift_covers_interval(s_min, slot_min):
                        count += 1
                coverage[d, slot] = count

        understaffed = sum(
            1 for (d, slot), req in required.items()
            if req > 0 and coverage.get((d, slot), 0) < req
        )

        return {
            "status_label": status_label,
            "objective_value": float(solver.ObjectiveValue()),
            "total_understaffed": understaffed,
            "assignments": assignments,
            "coverage": coverage,
        }

    # ----- DB writers -----------------------------------------------
    def _create_schedule_row(
        self,
        staffing_id: int,
        name: str,
        start_date,
        horizon_days: int,
    ) -> int:
        row = self.db.execute(
            text("""
                INSERT INTO schedules
                    (name, start_date, end_date, status, staffing_id,
                     solver_status, started_at)
                VALUES (:name, :start, :end, 'draft', :sid, 'running', NOW())
                RETURNING id
            """),
            {
                "name": name,
                "start": start_date,
                "end": start_date + timedelta(days=horizon_days),
                "sid": staffing_id,
            },
        ).fetchone()
        self.db.commit()
        return int(row[0])

    def _write_shift_segments(
        self,
        schedule_id: int,
        agents: list,
        assignments: dict[tuple[int, int], int | None],
        first_day: datetime,
    ) -> int:
        # Wipe any stale segments first.
        self.db.execute(
            text("DELETE FROM shift_segments WHERE schedule_id = :id"),
            {"id": schedule_id},
        )

        rows = []
        for (a_idx, d), start_min in assignments.items():
            if start_min is None:
                continue
            agent_id = int(agents[a_idx]["id"])
            shift_start = first_day + timedelta(days=d, minutes=start_min)
            shift_end = shift_start + timedelta(minutes=SHIFT_LENGTH_MIN)
            rows.append({
                "sid": schedule_id,
                "aid": agent_id,
                "stype": "work",
                "start": shift_start,
                "end": shift_end,
            })

        if rows:
            self.db.execute(
                text("""
                    INSERT INTO shift_segments
                        (schedule_id, agent_id, segment_type, start_time, end_time)
                    VALUES (:sid, :aid, :stype, :start, :end)
                """),
                rows,
            )
        self.db.commit()
        return len(rows)

    def _write_coverage(
        self,
        schedule_id: int,
        horizon_days: int,
        first_day: datetime,
        required: dict[tuple[int, int], int],
        coverage: dict[tuple[int, int], int],
    ) -> int:
        self.db.execute(
            text("DELETE FROM schedule_coverage WHERE schedule_id = :id"),
            {"id": schedule_id},
        )

        rows = []
        for d in range(horizon_days):
            for slot in range(INTERVALS_PER_DAY):
                req = required.get((d, slot), 0)
                cov = coverage.get((d, slot), 0)
                # Only persist rows with meaningful data (req > 0 OR cov > 0)
                if req == 0 and cov == 0:
                    continue
                ts = first_day + timedelta(days=d, minutes=slot * INTERVAL_MIN)
                rows.append({
                    "sid": schedule_id,
                    "ds": ts,
                    "req": req,
                    "sched": cov,
                    "short": max(0, req - cov),
                })

        if rows:
            self.db.execute(
                text("""
                    INSERT INTO schedule_coverage
                        (schedule_id, interval_start,
                         required_agents, scheduled_agents, shortage)
                    VALUES (:sid, :ds, :req, :sched, :short)
                """),
                rows,
            )
        self.db.commit()
        return len(rows)

    def _mark_completed(
        self,
        schedule_id: int,
        solver_status: str,
        runtime_seconds: float,
        objective_value: float,
        total_understaffed_intervals: int,
    ) -> None:
        self.db.execute(
            text("""
                UPDATE schedules SET
                    solver_status = :status,
                    solver_runtime_seconds = :rt,
                    objective_value = :obj,
                    total_understaffed_intervals = :under,
                    completed_at = NOW(),
                    status = CASE WHEN :status IN ('optimal','feasible')
                                  THEN 'published' ELSE status END
                WHERE id = :id
            """),
            {
                "id": schedule_id,
                "status": solver_status,
                "rt": round(runtime_seconds, 2),
                "obj": objective_value,
                "under": total_understaffed_intervals,
            },
        )
        self.db.commit()

    def _mark_failed(self, schedule_id: int, msg: str, runtime_seconds: float) -> None:
        self.db.execute(
            text("""
                UPDATE schedules SET
                    solver_status = 'failed',
                    error_message = :msg,
                    solver_runtime_seconds = :rt,
                    completed_at = NOW()
                WHERE id = :id
            """),
            {"id": schedule_id, "msg": msg[:1000], "rt": round(runtime_seconds, 2)},
        )
        self.db.commit()
