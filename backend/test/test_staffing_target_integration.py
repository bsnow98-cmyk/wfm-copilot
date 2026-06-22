"""
Postgres-backed integration tests for Surface #5 (staffing-target recompute).

Exercises the real Erlang C recompute against a throwaway forecast run + staffing
scenario, in a rolled-back transaction. SKIPPED when no DB is reachable.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services import staffing_target as ST


def _database_url() -> str | None:
    user = os.environ.get("POSTGRES_USER")
    pwd = os.environ.get("POSTGRES_PASSWORD")
    dbname = os.environ.get("POSTGRES_DB")
    if not (user and pwd and dbname):
        return None
    host = os.environ.get("TEST_POSTGRES_HOST", "localhost")
    port = os.environ.get("TEST_POSTGRES_PORT", "5432")
    return f"postgresql+psycopg://{user}:{pwd}@{host}:{port}/{dbname}"


@pytest.fixture(scope="module")
def engine():
    url = _database_url()
    if url is None:
        pytest.skip("POSTGRES_* env vars not set — skipping live-DB integration tests")
    eng = create_engine(url, future=True)
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable ({type(exc).__name__}) — skipping integration tests")
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    conn = engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, future=True)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def fixture_staffing(db: Session) -> dict:
    """A throwaway completed forecast run with a few busy intervals + a staffing
    scenario at 80%/20s, ASA 30, shrink 30%."""
    run_id = db.execute(
        text(
            """
            INSERT INTO forecast_runs (queue, channel, model_name, horizon_start,
                                       horizon_end, status)
            VALUES ('itest_st', 'voice', 'itest', :hs, :he, 'completed')
            RETURNING id
            """
        ),
        {"hs": datetime(2099, 2, 1, tzinfo=timezone.utc), "he": datetime(2099, 2, 2, tzinfo=timezone.utc)},
    ).scalar_one()
    base = datetime(2099, 2, 1, 9, 0, tzinfo=timezone.utc)
    for i in range(6):
        db.execute(
            text(
                """
                INSERT INTO forecast_intervals (forecast_run_id, interval_start,
                                                forecast_offered, forecast_aht_seconds)
                VALUES (:r, :ts, :off, 300)
                """
            ),
            {"r": run_id, "ts": base + timedelta(minutes=30 * i), "off": 200 + i * 20},
        )
    staffing_id = db.execute(
        text(
            """
            INSERT INTO staffing_requirements
                (forecast_run_id, service_level_target, target_answer_seconds,
                 shrinkage, target_asa_seconds)
            VALUES (:r, 0.80, 20, 0.30, 30)
            RETURNING id
            """
        ),
        {"r": run_id},
    ).scalar_one()
    # Seed the intervals at the baseline targets.
    ST.recompute_intervals(db, int(staffing_id), {
        "sl": 0.80, "target_answer_seconds": 20, "target_asa_seconds": 30, "shrinkage": 0.30,
    })
    return {"run_id": int(run_id), "staffing_id": int(staffing_id)}


def test_apply_defers_then_recompute_raises_peak(db: Session, fixture_staffing: dict) -> None:
    sid = fixture_staffing["staffing_id"]
    before, _ = ST.load_targets(db, sid)
    ver = ST.compute_targets_version(before)
    peak0 = ST.peak_required(db, sid)

    result = ST.apply_target_change(
        db, staffing_id=sid, new_targets={"sl": 0.90}, expected_version=ver, conversation_id=None
    )
    # Apply only writes the pending log — targets unchanged until recompute.
    assert result.peak_required_before == peak0
    cur_sl = db.execute(
        text("SELECT service_level_target FROM staffing_requirements WHERE id=:i"), {"i": sid}
    ).scalar()
    assert float(cur_sl) == pytest.approx(0.80)
    status = db.execute(
        text("SELECT recompute_status FROM staffing_target_change_log WHERE id=CAST(:i AS uuid)"),
        {"i": result.log_id},
    ).scalar()
    assert status == "pending"

    # Recompute (what the bg job does) raises the SL → peak should not drop.
    peak_after = ST.recompute_intervals(db, sid, result.after_targets)
    assert float(db.execute(text("SELECT service_level_target FROM staffing_requirements WHERE id=:i"), {"i": sid}).scalar()) == pytest.approx(0.90)
    assert peak_after >= peak0


def test_undo_restores_targets_and_intervals(db: Session, fixture_staffing: dict) -> None:
    sid = fixture_staffing["staffing_id"]
    before, _ = ST.load_targets(db, sid)
    peak0 = ST.peak_required(db, sid)
    result = ST.apply_target_change(
        db, staffing_id=sid, new_targets={"sl": 0.95}, expected_version=ST.compute_targets_version(before), conversation_id=None
    )
    ST.recompute_intervals(db, sid, result.after_targets)  # simulate job

    undo = ST.undo_target_change(db, result.log_id)
    assert undo.peak_required_after == peak0
    assert float(db.execute(text("SELECT service_level_target FROM staffing_requirements WHERE id=:i"), {"i": sid}).scalar()) == pytest.approx(0.80)


def test_stale_version_raises(db: Session, fixture_staffing: dict) -> None:
    sid = fixture_staffing["staffing_id"]
    stale = ST.compute_targets_version({"sl": 0.80, "target_answer_seconds": 20, "target_asa_seconds": 30, "shrinkage": 0.30})
    db.execute(text("UPDATE staffing_requirements SET service_level_target=0.92 WHERE id=:i"), {"i": sid})
    with pytest.raises(ST.StaleVersionError) as exc:
        ST.apply_target_change(db, staffing_id=sid, new_targets={"sl": 0.85}, expected_version=stale, conversation_id=None)
    assert exc.value.staffing_id == sid
