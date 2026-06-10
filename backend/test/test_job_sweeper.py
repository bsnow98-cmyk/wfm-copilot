"""
Integration tests for the orphaned-job sweeper (services/job_sweeper.py).

Runs against the live local Postgres (same pattern as test_wave3_4_smoke):
inserts synthetic stale/fresh rows, sweeps, asserts, and deletes its rows.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.services.job_sweeper import STALE_AFTER_MINUTES, sweep_orphaned_jobs


@pytest.fixture()
def db():
    user = os.environ.get("POSTGRES_USER", "wfm")
    pwd = os.environ.get("POSTGRES_PASSWORD", "wfm_dev_password")
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = os.environ.get("POSTGRES_DB", "wfm_copilot")
    url = f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{name}"
    try:
        engine = create_engine(url, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip(f"Postgres not reachable at {host}:{port}")
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()
    engine.dispose()


def _insert_schedule(db: Session, *, solver_status: str, age_minutes: int) -> int:
    row = db.execute(
        text("""
            INSERT INTO schedules
                (name, start_date, end_date, status, solver_status,
                 created_at, started_at)
            VALUES
                ('sweeper-test', CURRENT_DATE, CURRENT_DATE + 7, 'draft',
                 :ss,
                 NOW() - make_interval(mins => :age),
                 NOW() - make_interval(mins => :age))
            RETURNING id
        """),
        {"ss": solver_status, "age": age_minutes},
    ).fetchone()
    return int(row[0])


def _insert_forecast_run(db: Session, *, status: str, age_minutes: int) -> int:
    row = db.execute(
        text("""
            INSERT INTO forecast_runs
                (queue, channel, model_name, horizon_start, horizon_end,
                 status, created_at, started_at)
            VALUES
                ('sweeper-test', 'voice', 'mstl', NOW(), NOW() + interval '7 days',
                 :st,
                 NOW() - make_interval(mins => :age),
                 NOW() - make_interval(mins => :age))
            RETURNING id
        """),
        {"st": status, "age": age_minutes},
    ).fetchone()
    return int(row[0])


def test_sweeper_fails_stale_jobs_and_spares_fresh_ones(db: Session) -> None:
    stale_age = STALE_AFTER_MINUTES + 5
    ids = {
        "stale_sched": _insert_schedule(db, solver_status="running", age_minutes=stale_age),
        "stale_pending_sched": _insert_schedule(db, solver_status="pending", age_minutes=stale_age),
        "fresh_sched": _insert_schedule(db, solver_status="running", age_minutes=1),
        "done_sched": _insert_schedule(db, solver_status="optimal", age_minutes=stale_age),
        "stale_run": _insert_forecast_run(db, status="running", age_minutes=stale_age),
        "fresh_run": _insert_forecast_run(db, status="pending", age_minutes=1),
    }
    db.commit()

    try:
        counts = sweep_orphaned_jobs(db)
        # >= because real orphans may exist in the dev DB alongside ours.
        assert counts["schedules"] >= 2
        assert counts["forecast_runs"] >= 1

        def sched(field: str, id_: int):
            return db.execute(
                text(f"SELECT {field} FROM schedules WHERE id=:id"), {"id": id_}
            ).scalar_one()

        assert sched("solver_status", ids["stale_sched"]) == "failed"
        assert "interrupted" in sched("error_message", ids["stale_sched"])
        assert sched("completed_at", ids["stale_sched"]) is not None
        assert sched("solver_status", ids["stale_pending_sched"]) == "failed"
        # Fresh and finished rows are untouched.
        assert sched("solver_status", ids["fresh_sched"]) == "running"
        assert sched("solver_status", ids["done_sched"]) == "optimal"

        run_status = db.execute(
            text("SELECT status FROM forecast_runs WHERE id=:id"),
            {"id": ids["stale_run"]},
        ).scalar_one()
        assert run_status == "failed"
        fresh_status = db.execute(
            text("SELECT status FROM forecast_runs WHERE id=:id"),
            {"id": ids["fresh_run"]},
        ).scalar_one()
        assert fresh_status == "pending"
    finally:
        db.execute(
            text("DELETE FROM schedules WHERE name = 'sweeper-test'")
        )
        db.execute(
            text("DELETE FROM forecast_runs WHERE queue = 'sweeper-test'")
        )
        db.commit()
