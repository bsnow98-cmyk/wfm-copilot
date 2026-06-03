"""
Real pipeline seeder — replaces the deterministic skeleton schedule with a
genuine forecast -> Erlang C -> multi-skill CP-SAT run.

Why this exists: `seed_prod_skeleton.py` makes every agent work an identical
9-5 M-F block because MSTL + CP-SAT OOM the 512MB Render tier. A WFM-savvy
reviewer spots the uniformity instantly. This script runs the *real* math in
the local process (no Render OOM) against whatever DB DATABASE_URL points at,
and writes a schedule with varied starts, varied days-off, mid-shift breaks,
and skill-aware assignments.

Pipeline (all compute local; only rows land in the target DB):
  1. AGGREGATE MSTL forecast on AGG_QUEUE history -> Erlang C staffing.
     Drives the headline coverage curve + every aggregate read-side tool.
  2. PER-SKILL MSTL forecasts on SKILL_QUEUE history (one run per skill).
     Drives get_skills_coverage + the solver's per-skill demand.
  3. Build required[d,slot,skill] (Erlang C + substitution discount, in-memory)
     and aggregate_required[d,slot] (from the aggregate staffing).
  4. Multi-skill CP-SAT -> schedule + skill-tagged shift_segments (work/lunch/
     break exploded) + aggregate schedule_coverage.
  5. Anchor the sim clock to mid-schedule-week; regenerate Wave 3+4 data from
     the new shifts so adherence/PTO/training stay consistent.

Idempotent-ish: deletes prior real/skeleton forecast_runs + schedules for the
target queues before writing, so re-running replaces rather than stacks.

Prereqs in the target DB: 50 active agents with agent_skills (seed_agents
--multi-skill) and interval_history for BOTH queues:
  - AGG_QUEUE   : aggregate rows (skill_id NULL)
  - SKILL_QUEUE : per-skill rows (skill_id set) — needs migration 0017

Connect:
    DATABASE_URL=postgresql://...  python -m scripts.seed_prod_real
Local (no DATABASE_URL): falls back to app config (docker compose api container).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# ---- config: VERIFY these against the target DB's actual queue names ----
AGG_QUEUE = "all"
AGG_CHANNEL = "voice"
SKILL_QUEUE = "skills"
SKILL_CHANNEL = "voice"
SKILLS = ["sales", "support", "billing"]
FORECAST_HORIZON_DAYS = 14   # wide enough to fit a clean Mon-Fri week
SCHEDULE_DAYS = 7
MODEL = "mstl"
BACKTEST_DAYS = 14
SL_TARGET = 0.80
TARGET_ANSWER_SEC = 20
TARGET_ASA_SEC = 30
SHRINKAGE = 0.22
SOLVE_TIME_S = 90


def _engine_from_url(url: str):
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return create_engine(url, future=True)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    from app.config import get_settings

    return get_settings().database_url


def _skill_id(db: Session, name: str) -> int:
    sid = db.execute(
        text("SELECT id FROM skills WHERE name = :n"), {"n": name}
    ).scalar_one_or_none()
    if sid is None:
        raise SystemExit(
            f"skill {name!r} not found — run seed_agents --multi-skill first."
        )
    return int(sid)


def _next_monday(day: datetime) -> datetime:
    """First Monday at/after `day` (midnight UTC)."""
    base = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(days=(0 - base.weekday()) % 7)


def _clear_prior(db: Session) -> None:
    """Drop prior real/skeleton forecast_runs + schedules for our queues so
    re-runs replace cleanly. FK cascades remove staffing + segments + coverage.

    Order matters: delete schedules FIRST. A schedule references its
    staffing_requirements row (schedules_staffing_id_fkey, RESTRICT), and that
    staffing row cascades from forecast_runs. Deleting forecast_runs first
    therefore fails with a ForeignKeyViolation whenever a prior schedule still
    exists (e.g. the skeleton schedule on prod, or a prior real run locally)."""
    db.execute(
        text(
            "DELETE FROM schedules WHERE name LIKE 'Skeleton%' "
            "OR name LIKE 'Real CP-SAT%'"
        )
    )
    db.execute(
        text(
            "DELETE FROM forecast_runs WHERE queue IN (:agg, :skill)"
        ),
        {"agg": AGG_QUEUE, "skill": SKILL_QUEUE},
    )
    db.commit()


def _run_forecast(db: Session, *, queue: str, channel: str, skill_id: int | None) -> int:
    from app.services.forecasting import ForecastService

    fc = ForecastService(db)
    run_id = fc.create_run(
        queue=queue,
        channel=channel,
        horizon_days=FORECAST_HORIZON_DAYS,
        model=MODEL,
        backtest_days=BACKTEST_DAYS,
        skill_id=skill_id,
    )
    fc.execute_run(
        run_id,
        queue=queue,
        channel=channel,
        horizon_days=FORECAST_HORIZON_DAYS,
        model=MODEL,
        backtest_days=BACKTEST_DAYS,
        skill_id=skill_id,
    )
    status = db.execute(
        text("SELECT status, error_message FROM forecast_runs WHERE id = :id"),
        {"id": run_id},
    ).mappings().one()
    if status["status"] != "completed":
        raise SystemExit(
            f"forecast run {run_id} (queue={queue} skill={skill_id}) "
            f"failed: {status['error_message']}"
        )
    return run_id


def main() -> int:
    engine = _engine_from_url(_database_url())
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db: Session = SessionLocal()

    print("Clearing prior real/skeleton forecast_runs + schedules…")
    _clear_prior(db)

    # ---------- 1. aggregate forecast + staffing ----------
    print(f"Aggregate MSTL forecast on queue={AGG_QUEUE}…")
    agg_run = _run_forecast(db, queue=AGG_QUEUE, channel=AGG_CHANNEL, skill_id=None)

    first_ts = db.execute(
        text(
            "SELECT MIN(interval_start) AS ts FROM forecast_intervals "
            "WHERE forecast_run_id = :r"
        ),
        {"r": agg_run},
    ).scalar_one()
    first_forecast_day = first_ts.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    schedule_monday = _next_monday(first_forecast_day)
    print(
        f"  forecast starts {first_forecast_day.date()}, "
        f"schedule week = {schedule_monday.date()} (Mon) → "
        f"{(schedule_monday + timedelta(days=4)).date()} (Fri)"
    )

    from app.services.staffing import StaffingService

    staffing_id = StaffingService(db).compute(
        forecast_run_id=agg_run,
        service_level_target=SL_TARGET,
        target_answer_seconds=TARGET_ANSWER_SEC,
        shrinkage=SHRINKAGE,
        target_asa_seconds=TARGET_ASA_SEC,
    )
    print(f"  aggregate staffing_id={staffing_id}")

    # ---------- 2. per-skill forecasts ----------
    per_skill_run_ids: dict[int, int] = {}
    for skill in SKILLS:
        sid = _skill_id(db, skill)
        print(f"Per-skill MSTL forecast: {skill} (skill_id={sid})…")
        per_skill_run_ids[sid] = _run_forecast(
            db, queue=SKILL_QUEUE, channel=SKILL_CHANNEL, skill_id=sid
        )

    # ---------- 3. build demand ----------
    from app.services.multi_skill_demand import (
        build_aggregate_required,
        build_required_per_skill,
        load_agents_with_skills,
    )

    required_per_skill = build_required_per_skill(
        db,
        per_skill_run_ids=per_skill_run_ids,
        first_day=schedule_monday,
        horizon_days=SCHEDULE_DAYS,
        sl_target=SL_TARGET,
        target_answer_sec=TARGET_ANSWER_SEC,
        target_asa_sec=TARGET_ASA_SEC,
        shrinkage=SHRINKAGE,
    )
    aggregate_required = build_aggregate_required(
        db,
        staffing_id=staffing_id,
        first_day=schedule_monday,
        horizon_days=SCHEDULE_DAYS,
    )
    agents = load_agents_with_skills(db)
    print(
        f"  demand: {len(required_per_skill)} per-skill cells, "
        f"{len(aggregate_required)} aggregate cells, {len(agents)} agents"
    )
    if not agents:
        raise SystemExit("no active agents with skills — run seed_agents --multi-skill")
    if not required_per_skill:
        raise SystemExit(
            "per-skill demand is empty — check SKILL_QUEUE history + migration 0017"
        )

    # ---------- 4. multi-skill solve + persist ----------
    from app.services.scheduling_multi_skill_persist import MultiSkillScheduleService

    print("Solving multi-skill CP-SAT…")
    summary = MultiSkillScheduleService(db).solve_and_persist(
        agents=agents,
        required_per_skill=required_per_skill,
        aggregate_required=aggregate_required,
        first_day=schedule_monday,
        horizon_days=SCHEDULE_DAYS,
        staffing_id=staffing_id,
        name="Real CP-SAT week (multi-skill)",
        max_solve_time_seconds=SOLVE_TIME_S,
    )
    print(f"  {summary}")
    if summary["solver_status"] not in ("optimal", "feasible"):
        raise SystemExit(f"solver returned {summary['solver_status']} — aborting")

    # ---------- 5. anchor sim clock + Wave 3+4 ----------
    from app.services.realtime_clock import reset_anchor

    sim_anchor_ts = schedule_monday + timedelta(days=2, hours=14)  # Wed 14:00 UTC
    reset_anchor(
        db,
        anchor_sim_ts=sim_anchor_ts,
        speed_multiplier=1.0,
        notes="Reset by seed_prod_real",
    )
    print(f"  sim_now anchored to {sim_anchor_ts.isoformat()}")

    db.execute(
        text(
            """
            UPDATE agents
               SET hire_date = sim_now()::date - (30 + (id * 41) % 1800)::int
             WHERE hire_date IS NULL
            """
        )
    )
    db.commit()

    import random

    import scripts.generate_wave3_4_data as g

    rng = random.Random(42)
    sim_now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    half = timedelta(days=14)
    win_start, win_end = sim_now - half, sim_now + half
    print(f"Regenerating Wave 3+4 ({win_start.date()} → {win_end.date()})…")
    g.truncate_wave_tables(db)
    shifts = g.fetch_shifts_window(db, win_start, win_end)
    aux_n, exc_n = g.generate_aux_events_and_exceptions(
        db, shifts, rng=rng, deviation_rate=0.15
    )
    pto_n, leave_n = g.generate_pto_and_leave(db, sim_now=sim_now, rng=rng)
    counts = g.generate_training_certs_qa(db, sim_now=sim_now, rng=rng)
    print(
        f"  shifts={len(shifts)} aux={aux_n} exc={exc_n} "
        f"pto={pto_n} leave={leave_n} {counts}"
    )

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
