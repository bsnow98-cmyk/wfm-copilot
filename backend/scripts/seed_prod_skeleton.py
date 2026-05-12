"""
One-off prod seeder — Wave 3+4 demo skeleton.

Prod's free Postgres has the schema but none of the Phase 1-4 data
(shift_segments, staffing, coverage, interval_history are empty), so
the Wave 3+4 tools all return zero rows. Running real MSTL+CP-SAT would
OOM the 512MB tier. So this script produces a *deterministic skeleton*:

  - Resets sim_anchor to a Wednesday 14:00 UTC inside a fresh 5-day week
  - Backfills agents.hire_date for the 50 existing agents
  - Creates one published `schedules` row for that week
  - Inserts shift_segments: every agent × Mon-Fri × 5 segments per day
    (work 9-12, lunch 12-12:30, work 12:30-15, break 15-15:15, work 15:15-17)
  - Creates one forecast_runs + staffing_requirements
  - Fills staffing_requirement_intervals + schedule_coverage for the
    16 × 5 = 80 intervals across the week (req=10, scheduled=50)
  - Fills interval_history + forecast_intervals for the same intervals
  - Then calls the Wave 3+4 generator's functions to produce aux events,
    PTO ledger, leave requests, training events, certs, QA, new-hire class

Idempotent-ish: truncates the Wave 3+4 tables on every run. Does NOT
truncate Phase 1-4 tables — if you re-run, you'll get a SECOND schedule
on top of the first. Run once.

Connect via the DATABASE_URL env var (the prod External Database URL,
including ?sslmode=require). Example invocation in the docker one-off
block at the bottom of the file's docstring is:

    docker run --rm -v "$(pwd)/backend:/app" -w /app \\
      -e DATABASE_URL="postgresql://...@...render.com/wfm_copilot_db?sslmode=require" \\
      wfm-copilot-api python -m scripts.seed_prod_skeleton
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker


def _engine_from_url(url: str):
    # SQLAlchemy uses `postgresql+psycopg://` for psycopg3.
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return create_engine(url, future=True)


def reset_sim_anchor(db: Session, anchor_sim_ts: datetime) -> None:
    db.execute(
        text(
            """
            UPDATE sim_anchor
               SET anchor_real_ts = NOW(),
                   anchor_sim_ts = :ts,
                   speed_multiplier = 1.0,
                   notes = 'Reset by seed_prod_skeleton',
                   updated_at = NOW()
             WHERE id = TRUE
            """
        ),
        {"ts": anchor_sim_ts},
    )
    db.commit()


def backfill_hire_date(db: Session) -> int:
    res = db.execute(
        text(
            """
            UPDATE agents
               SET hire_date = sim_now()::date - (30 + (id * 41) % 1800)::int
             WHERE hire_date IS NULL
            """
        )
    )
    db.commit()
    return res.rowcount


def insert_schedule_and_shifts(db: Session, monday: datetime) -> int:
    """Returns the new schedule_id."""
    schedule_id = db.execute(
        text(
            """
            INSERT INTO schedules (name, start_date, end_date, status)
            VALUES (:name, :start, :end, 'published')
            RETURNING id
            """
        ),
        {
            "name": "Skeleton week for Wave 3+4 demo",
            "start": monday.date(),
            "end": (monday + timedelta(days=4)).date(),
        },
    ).scalar_one()

    # 5 segments per agent per weekday. Hours are UTC: 13:00-21:00 UTC
    # corresponds to 9am-5pm US Eastern (assuming summer DST). sim_anchor
    # is set to Wed 14:00 UTC = 10am ET, mid-morning shift.
    segments = [
        ("work", timedelta(hours=13), timedelta(hours=16)),
        ("lunch", timedelta(hours=16), timedelta(hours=16, minutes=30)),
        ("work", timedelta(hours=16, minutes=30), timedelta(hours=19)),
        ("break", timedelta(hours=19), timedelta(hours=19, minutes=15)),
        ("work", timedelta(hours=19, minutes=15), timedelta(hours=21)),
    ]
    agents = (
        db.execute(text("SELECT id FROM agents WHERE active = TRUE ORDER BY id"))
        .mappings()
        .all()
    )
    # Per-agent default skill (first agent_skills row, or NULL if none).
    skill_for_agent: dict[int, int | None] = {}
    rows = (
        db.execute(
            text(
                "SELECT DISTINCT ON (agent_id) agent_id, skill_id "
                "FROM agent_skills ORDER BY agent_id, proficiency DESC"
            )
        )
        .mappings()
        .all()
    )
    for r in rows:
        skill_for_agent[r["agent_id"]] = r["skill_id"]

    batch = []
    for day_offset in range(5):  # Mon..Fri
        day_base = monday + timedelta(days=day_offset)
        for a in agents:
            for stype, start_off, end_off in segments:
                batch.append(
                    {
                        "schedule_id": schedule_id,
                        "agent_id": a["id"],
                        "segment_type": stype,
                        "start_time": day_base + start_off,
                        "end_time": day_base + end_off,
                        "skill_id": skill_for_agent.get(a["id"]),
                    }
                )
    db.execute(
        text(
            """
            INSERT INTO shift_segments
                (schedule_id, agent_id, segment_type, start_time, end_time, skill_id)
            VALUES
                (:schedule_id, :agent_id, :segment_type, :start_time, :end_time, :skill_id)
            """
        ),
        batch,
    )
    db.commit()
    print(f"  inserted {len(batch)} shift_segments under schedule_id={schedule_id}")
    return schedule_id


def insert_forecast_and_staffing(
    db: Session, monday: datetime, schedule_id: int
) -> None:
    horizon_start = monday
    horizon_end = monday + timedelta(days=5)

    forecast_run_id = db.execute(
        text(
            """
            INSERT INTO forecast_runs
                (queue, channel, model_name, horizon_start, horizon_end, mape, wape, notes)
            VALUES
                ('all', 'voice', 'skeleton', :hs, :he, 0.084, 0.067, 'Skeleton seed')
            RETURNING id
            """
        ),
        {"hs": horizon_start, "he": horizon_end},
    ).scalar_one()

    staffing_id = db.execute(
        text(
            """
            INSERT INTO staffing_requirements
                (forecast_run_id, service_level_target, target_answer_seconds,
                 shrinkage, interval_minutes)
            VALUES
                (:fc, 0.80, 20, 0.30, 30)
            RETURNING id
            """
        ),
        {"fc": forecast_run_id},
    ).scalar_one()

    # Update schedules.staffing_id (FK was set up in 0005_schedule_links).
    db.execute(
        text("UPDATE schedules SET staffing_id = :st WHERE id = :sched"),
        {"st": staffing_id, "sched": schedule_id},
    )

    # 16 intervals per day (13:00-21:00 UTC = 9am-5pm ET, 30-min steps),
    # 5 weekdays = 80.
    intervals: list[datetime] = []
    for day_offset in range(5):
        day_base = monday + timedelta(days=day_offset)
        for half_hour in range(16):
            intervals.append(day_base + timedelta(hours=13, minutes=30 * half_hour))

    # staffing_requirement_intervals: req=10, raw=10
    db.execute(
        text(
            """
            INSERT INTO staffing_requirement_intervals
                (staffing_id, interval_start, forecast_offered, forecast_aht_seconds,
                 required_agents_raw, required_agents, expected_service_level,
                 expected_asa_seconds, occupancy)
            VALUES
                (:st, :ts, 100, 360, 10, 10, 0.82, 18, 0.78)
            """
        ),
        [{"st": staffing_id, "ts": ts} for ts in intervals],
    )

    # schedule_coverage: required=10, scheduled=50
    db.execute(
        text(
            """
            INSERT INTO schedule_coverage
                (schedule_id, interval_start, required_agents, scheduled_agents)
            VALUES
                (:sched, :ts, 10, 50)
            """
        ),
        [{"sched": schedule_id, "ts": ts} for ts in intervals],
    )

    # forecast_intervals: forecast_offered=100, with slight variation per interval
    db.execute(
        text(
            """
            INSERT INTO forecast_intervals
                (forecast_run_id, interval_start, forecast_offered)
            VALUES
                (:fc, :ts, :off)
            """
        ),
        [
            {
                "fc": forecast_run_id,
                "ts": ts,
                "off": 80 + 40 * (1 if 11 <= ts.hour <= 14 else 0),
            }
            for ts in intervals
        ],
    )

    # interval_history: actuals close to forecast, ~80% SL
    db.execute(
        text(
            """
            INSERT INTO interval_history
                (queue, channel, interval_start, interval_minutes, offered, handled,
                 abandoned, aht_seconds, asa_seconds, service_level)
            VALUES
                ('all', 'voice', :ts, 30, :off, :hand, :ab, 360, 18, 0.82)
            ON CONFLICT (queue, channel, interval_start) DO NOTHING
            """
        ),
        [
            {
                "ts": ts,
                "off": (off := 80 + 40 * (1 if 11 <= ts.hour <= 14 else 0)),
                "hand": int(off * 0.96),
                "ab": int(off * 0.04),
            }
            for ts in intervals
        ],
    )
    db.commit()
    print(
        f"  inserted forecast_run={forecast_run_id}, staffing={staffing_id}, "
        f"{len(intervals)} intervals × 4 tables"
    )


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: DATABASE_URL env var required.", file=sys.stderr)
        return 2
    engine = _engine_from_url(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db: Session = SessionLocal()

    # ---------- 1. anchor the sim clock ----------
    # Pick a recent Wednesday at 14:00 UTC. Use the last completed Wednesday
    # relative to wall-clock now, so the skeleton week is "this past week".
    real_now = datetime.now(timezone.utc)
    days_since_wed = (real_now.weekday() - 2) % 7
    wed = (real_now - timedelta(days=days_since_wed)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    # IMPORTANT: monday must be at 00:00 UTC. If we just do `wed - 2 days`,
    # monday inherits wed's 14:00, and then `day_base + timedelta(hours=N)`
    # below puts shifts at hours 23:00-21:00+1d instead of 09:00-17:00 same
    # day. Normalize to midnight UTC.
    monday = (wed - timedelta(days=2)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    print(f"Skeleton week: {monday.date()} (Mon) → {(monday + timedelta(days=4)).date()} (Fri)")
    print(f"sim_now will tick from: {wed.isoformat()}")
    reset_sim_anchor(db, anchor_sim_ts=wed)

    # ---------- 2. backfill hire_date ----------
    n = backfill_hire_date(db)
    print(f"Backfilled hire_date on {n} agents")

    # ---------- 3. skeleton schedule + shifts ----------
    schedule_id = insert_schedule_and_shifts(db, monday)

    # ---------- 4. forecast + staffing + coverage + intervals ----------
    insert_forecast_and_staffing(db, monday, schedule_id)

    # ---------- 5. Wave 3+4 generator on top ----------
    import scripts.generate_wave3_4_data as g  # noqa: E402

    rng = random.Random(42)
    sim_now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]
    half = timedelta(days=14)
    win_start, win_end = sim_now - half, sim_now + half
    print(f"Wave 3+4 generator window: {win_start.date()} → {win_end.date()}")

    print("Truncating Wave 3+4 tables…")
    g.truncate_wave_tables(db)

    print("Fetching shift_segments…")
    shifts = g.fetch_shifts_window(db, win_start, win_end)
    print(f"  {len(shifts)} segments")

    print("Generating aux + exceptions…")
    aux_n, exc_n = g.generate_aux_events_and_exceptions(
        db, shifts, rng=rng, deviation_rate=0.15
    )
    print(f"  aux_events={aux_n}, exceptions={exc_n}")

    print("Generating PTO + leave…")
    pto_n, leave_n = g.generate_pto_and_leave(db, sim_now=sim_now, rng=rng)
    print(f"  pto={pto_n}, leave={leave_n}")

    print("Generating training, certs, QA, new-hire class…")
    counts = g.generate_training_certs_qa(db, sim_now=sim_now, rng=rng)
    print(f"  {counts}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
