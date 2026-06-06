"""
Phase 8 Stage 4 — persistence wrapper over the multi-skill CP-SAT solver.

`scheduling_multi_skill.solve_multi_skill` is pure math (no DB). Its docstring
flags the gap: "DB I/O ... a multi-skill version of [ScheduleService] would be a
service-layer wrapper over solve_multi_skill ... stage 4 territory." This module
is that wrapper.

What it writes
--------------
- one `schedules` row (running -> published) linked to the AGGREGATE staffing_id
- `shift_segments` with skill_id set, each 8h block EXPLODED into
  work/lunch/break sub-segments so Wave 3+4 adherence (which keys off break/lunch
  segment_type) stays rich and the Gantt shows mid-shift breaks
- AGGREGATE `schedule_coverage` (skill_id NULL, one row per interval) — the shape
  get_intraday_gaps / get_occupancy / recommend_* expect. Per-skill richness lives
  in shift_segments.skill_id + per-skill forecast_runs, NOT in coverage (writing
  per-skill coverage rows would fan out the tools' interval_start-only joins).

The per-skill demand the SOLVER optimizes against (required[d, slot, skill_id])
is computed in-memory by the caller (seed_prod_real) and passed in — we never
persist per-skill staffing rows, so the aggregate staffing the tools read stays
the only staffing_requirement_intervals set.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import TypedDict

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.scheduling_multi_skill import (
    AgentWithSkills,
    SHIFT_LENGTH_MIN,
    solve_multi_skill,
)

log = logging.getLogger("wfm.scheduling.multi_skill.persist")

INTERVAL_MIN = 30
INTERVALS_PER_DAY = 24 * 60 // INTERVAL_MIN  # 48

# Break/lunch layout inside an 8h (480 min) shift, as minute offsets from the
# shift start, anchored to each agent's solved start time. Two paid 15-min
# breaks bracketing an unpaid 30-min lunch — the standard 8h contact-center
# rest pattern (mid-morning break, midday lunch, mid-afternoon break):
#   work   : 0   .. 120   (2h)
#   break1 : 120 .. 135   (15m)
#   work   : 135 .. 255   (2h)
#   lunch  : 255 .. 285   (30m)
#   work   : 285 .. 405   (2h)
#   break2 : 405 .. 420   (15m)
#   work   : 420 .. 480   (1h)
# => 7h work + 2x15m break + 30m lunch = 480 min.
_BREAK1_OFFSET = 120
_BREAK1_LEN = 15
_LUNCH_OFFSET = 255
_LUNCH_LEN = 30
_BREAK2_OFFSET = 405
_BREAK2_LEN = 15


def _segments_for_shift(start_min: int) -> list[tuple[str, int, int]]:
    """(segment_type, start_offset_min, end_offset_min) for one 8h shift,
    offsets measured from day-midnight (start_min is the shift start)."""
    s = start_min
    break1_s = s + _BREAK1_OFFSET
    break1_e = break1_s + _BREAK1_LEN
    lunch_s = s + _LUNCH_OFFSET
    lunch_e = lunch_s + _LUNCH_LEN
    break2_s = s + _BREAK2_OFFSET
    break2_e = break2_s + _BREAK2_LEN
    shift_e = s + SHIFT_LENGTH_MIN
    return [
        ("work", s, break1_s),
        ("break", break1_s, break1_e),
        ("work", break1_e, lunch_s),
        ("lunch", lunch_s, lunch_e),
        ("work", lunch_e, break2_s),
        ("break", break2_s, break2_e),
        ("work", break2_e, shift_e),
    ]


def _work_windows(start_min: int) -> list[tuple[int, int]]:
    return [
        (s, e) for stype, s, e in _segments_for_shift(start_min) if stype == "work"
    ]


class MultiSkillScheduleSummary(TypedDict):
    schedule_id: int
    solver_status: str
    runtime_seconds: float
    objective_value: float
    total_understaffed_intervals: int
    shift_segments_written: int
    coverage_rows_written: int


class MultiSkillScheduleService:
    def __init__(self, db: Session):
        self.db = db

    def solve_and_persist(
        self,
        *,
        agents: list[AgentWithSkills],
        required_per_skill: dict[tuple[int, int, int], float],
        aggregate_required: dict[tuple[int, int], int],
        first_day: datetime,
        horizon_days: int,
        staffing_id: int | None,
        name: str,
        target_shifts_per_week: int = 5,
        min_rest_hours: int = 11,
        max_consecutive_days: int = 6,
        max_solve_time_seconds: int = 90,
    ) -> MultiSkillScheduleSummary:
        """Run the multi-skill solver and persist schedule + shifts + coverage.

        `first_day` must be midnight UTC of the schedule's first day.
        `required_per_skill[(d, slot, skill_id)]` drives the solver.
        `aggregate_required[(d, slot)]` drives the headline coverage curve.
        """
        t_start = time.time()
        schedule_id = self._create_schedule_row(
            name=name,
            start_date=first_day.date(),
            horizon_days=horizon_days,
            staffing_id=staffing_id,
        )

        try:
            result = solve_multi_skill(
                agents=agents,
                horizon_days=horizon_days,
                required=required_per_skill,
                target_shifts_per_week=target_shifts_per_week,
                min_rest_hours=min_rest_hours,
                max_consecutive_days=max_consecutive_days,
                max_solve_time_seconds=max_solve_time_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(schedule_id, str(exc), time.time() - t_start)
            log.exception("Multi-skill solve failed")
            raise

        if result["status"] in ("infeasible", "failed"):
            self._mark_failed(
                schedule_id,
                f"solver returned {result['status']}",
                time.time() - t_start,
            )
            return MultiSkillScheduleSummary(
                schedule_id=schedule_id,
                solver_status=result["status"],
                runtime_seconds=round(time.time() - t_start, 2),
                objective_value=result["objective_value"],
                total_understaffed_intervals=result["total_understaffed_intervals"],
                shift_segments_written=0,
                coverage_rows_written=0,
            )

        segments_written = self._write_shift_segments(
            schedule_id=schedule_id,
            assignments=result["assignments"],
            first_day=first_day,
        )
        coverage_written = self._write_aggregate_coverage(
            schedule_id=schedule_id,
            assignments=result["assignments"],
            aggregate_required=aggregate_required,
            first_day=first_day,
            horizon_days=horizon_days,
        )

        runtime = time.time() - t_start
        self._mark_completed(
            schedule_id=schedule_id,
            solver_status=result["status"],
            runtime_seconds=runtime,
            objective_value=result["objective_value"],
            total_understaffed_intervals=result["total_understaffed_intervals"],
        )
        log.info(
            "Multi-skill schedule %s solved (%s) in %.1fs — segments=%d coverage=%d",
            schedule_id, result["status"], runtime, segments_written, coverage_written,
        )
        return MultiSkillScheduleSummary(
            schedule_id=schedule_id,
            solver_status=result["status"],
            runtime_seconds=round(runtime, 2),
            objective_value=result["objective_value"],
            total_understaffed_intervals=result["total_understaffed_intervals"],
            shift_segments_written=segments_written,
            coverage_rows_written=coverage_written,
        )

    # ----- DB writers ----------------------------------------------------
    def _create_schedule_row(
        self, *, name: str, start_date, horizon_days: int, staffing_id: int | None
    ) -> int:
        row = self.db.execute(
            text(
                """
                INSERT INTO schedules
                    (name, start_date, end_date, status, staffing_id,
                     solver_status, started_at)
                VALUES (:name, :start, :end, 'draft', :sid, 'running', NOW())
                RETURNING id
                """
            ),
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
        *,
        schedule_id: int,
        assignments: dict[tuple[int, int], tuple[int, int] | None],
        first_day: datetime,
    ) -> int:
        self.db.execute(
            text("DELETE FROM shift_segments WHERE schedule_id = :id"),
            {"id": schedule_id},
        )
        rows: list[dict] = []
        for (agent_id, d), picked in assignments.items():
            if picked is None:
                continue
            start_min, skill_id = picked
            for stype, off_s, off_e in _segments_for_shift(start_min):
                rows.append(
                    {
                        "sid": schedule_id,
                        "aid": int(agent_id),
                        "stype": stype,
                        "start": first_day + timedelta(days=d, minutes=off_s),
                        "end": first_day + timedelta(days=d, minutes=off_e),
                        "skill_id": int(skill_id),
                    }
                )
        if rows:
            self.db.execute(
                text(
                    """
                    INSERT INTO shift_segments
                        (schedule_id, agent_id, segment_type, start_time, end_time, skill_id)
                    VALUES (:sid, :aid, :stype, :start, :end, :skill_id)
                    """
                ),
                rows,
            )
        self.db.commit()
        return len(rows)

    def _write_aggregate_coverage(
        self,
        *,
        schedule_id: int,
        assignments: dict[tuple[int, int], tuple[int, int] | None],
        aggregate_required: dict[tuple[int, int], int],
        first_day: datetime,
        horizon_days: int,
    ) -> int:
        """Headline coverage: one row per interval (skill_id NULL). scheduled =
        count of agents in a WORK sub-segment overlapping the slot (breaks/lunch
        punch realistic dips). required = aggregate Erlang requirement."""
        self.db.execute(
            text("DELETE FROM schedule_coverage WHERE schedule_id = :id"),
            {"id": schedule_id},
        )

        # Pre-bucket each agent-day's work windows by day for fast slot counting.
        work_by_day: dict[int, list[tuple[int, int]]] = {}
        for (agent_id, d), picked in assignments.items():
            if picked is None:
                continue
            start_min, _skill = picked
            work_by_day.setdefault(d, []).extend(_work_windows(start_min))

        rows: list[dict] = []
        for d in range(horizon_days):
            windows = work_by_day.get(d, [])
            for slot in range(INTERVALS_PER_DAY):
                slot_min = slot * INTERVAL_MIN
                req = int(aggregate_required.get((d, slot), 0))
                scheduled = sum(1 for ws, we in windows if ws <= slot_min < we)
                if req == 0 and scheduled == 0:
                    continue
                rows.append(
                    {
                        "sid": schedule_id,
                        "ds": first_day + timedelta(days=d, minutes=slot_min),
                        "req": req,
                        "sched": scheduled,
                        "short": max(0, req - scheduled),
                    }
                )
        if rows:
            self.db.execute(
                text(
                    """
                    INSERT INTO schedule_coverage
                        (schedule_id, interval_start, required_agents,
                         scheduled_agents, shortage)
                    VALUES (:sid, :ds, :req, :sched, :short)
                    """
                ),
                rows,
            )
        self.db.commit()
        return len(rows)

    def _mark_completed(
        self,
        *,
        schedule_id: int,
        solver_status: str,
        runtime_seconds: float,
        objective_value: float,
        total_understaffed_intervals: int,
    ) -> None:
        self.db.execute(
            text(
                """
                UPDATE schedules SET
                    solver_status = :status,
                    solver_runtime_seconds = :rt,
                    objective_value = :obj,
                    total_understaffed_intervals = :under,
                    completed_at = NOW(),
                    status = CASE WHEN :status IN ('optimal','feasible')
                                  THEN 'published' ELSE status END
                WHERE id = :id
                """
            ),
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
            text(
                """
                UPDATE schedules SET
                    solver_status = 'failed',
                    error_message = :msg,
                    solver_runtime_seconds = :rt,
                    completed_at = NOW()
                WHERE id = :id
                """
            ),
            {"id": schedule_id, "msg": msg[:1000], "rt": round(runtime_seconds, 2)},
        )
        self.db.commit()
