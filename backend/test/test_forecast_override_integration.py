"""
Postgres-backed integration tests for Surface #4 (forecast overrides).
SKIPPED when no DB is reachable; runs in a rolled-back transaction.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services import forecast_override as F
from app.services import notifications


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
def fixture_interval(db: Session) -> dict:
    """A throwaway forecast run + one interval, far in the future."""
    run_id = db.execute(
        text(
            """
            INSERT INTO forecast_runs (queue, channel, model_name, horizon_start,
                                       horizon_end, status)
            VALUES ('itest_q', 'voice', 'itest', :hs, :he, 'completed')
            RETURNING id
            """
        ),
        {"hs": datetime(2099, 1, 1, tzinfo=timezone.utc), "he": datetime(2099, 1, 2, tzinfo=timezone.utc)},
    ).scalar_one()
    ts = datetime(2099, 1, 1, 9, 0, tzinfo=timezone.utc)
    db.execute(
        text(
            """
            INSERT INTO forecast_intervals (forecast_run_id, interval_start, forecast_offered)
            VALUES (:r, :ts, 100.0)
            """
        ),
        {"r": run_id, "ts": ts},
    )
    return {"run_id": int(run_id), "ts": ts.isoformat()}


def _val(db, run_id, ts):
    return F.load_interval_value(db, run_id, ts)


def test_apply_then_undo_round_trips(db: Session, fixture_interval: dict) -> None:
    rid, ts = fixture_interval["run_id"], fixture_interval["ts"]
    ver = F.compute_value_version(100.0)
    result = F.apply_override(
        db, forecast_run_id=rid, interval_start=ts, new_value=320.0,
        expected_version=ver, conversation_id=None,
    )
    assert _val(db, rid, ts) == pytest.approx(320.0)
    assert result.before_value == pytest.approx(100.0)

    log = db.execute(
        text(
            "SELECT before_value, after_value, undo_window_ends_at, applied_at "
            "FROM forecast_override_log WHERE id = CAST(:id AS uuid)"
        ),
        {"id": result.log_id},
    ).mappings().one()
    assert float(log["before_value"]) == pytest.approx(100.0)
    assert float(log["after_value"]) == pytest.approx(320.0)
    window = log["undo_window_ends_at"] - log["applied_at"]
    assert timedelta(hours=23, minutes=59) < window < timedelta(hours=24, minutes=1)

    notifications.notify_forecast_override_applied(
        db, summary=result.summary, log_id=result.log_id, forecast_run_id=rid, conversation_id=None
    )
    feed, unread = notifications.list_notifications(db, limit=10)
    assert any(n["category"] == "forecast_override_applied" for n in feed)

    F.undo_override(db, result.log_id)
    assert _val(db, rid, ts) == pytest.approx(100.0)
    undone = db.execute(
        text("SELECT undone_at FROM forecast_override_log WHERE id = CAST(:id AS uuid)"),
        {"id": result.log_id},
    ).scalar()
    assert undone is not None


def test_stale_version_raises(db: Session, fixture_interval: dict) -> None:
    rid, ts = fixture_interval["run_id"], fixture_interval["ts"]
    stale = F.compute_value_version(100.0)
    # Out-of-band change between preview and apply.
    db.execute(
        text("UPDATE forecast_intervals SET forecast_offered = 250 WHERE forecast_run_id=:r AND interval_start=:t"),
        {"r": rid, "t": ts},
    )
    with pytest.raises(F.StaleVersionError) as exc:
        F.apply_override(
            db, forecast_run_id=rid, interval_start=ts, new_value=320.0,
            expected_version=stale, conversation_id=None,
        )
    assert exc.value.your_version == stale
    assert exc.value.forecast_run_id == rid


def test_interval_not_found(db: Session, fixture_interval: dict) -> None:
    rid = fixture_interval["run_id"]
    with pytest.raises(F.IntervalNotFound):
        F.apply_override(
            db, forecast_run_id=rid, interval_start="2099-01-01T23:30:00+00:00",
            new_value=10.0, expected_version=0, conversation_id=None,
        )


def test_undo_outside_window_raises(db: Session, fixture_interval: dict) -> None:
    rid, ts = fixture_interval["run_id"], fixture_interval["ts"]
    result = F.apply_override(
        db, forecast_run_id=rid, interval_start=ts, new_value=320.0,
        expected_version=F.compute_value_version(100.0), conversation_id=None,
    )
    db.execute(
        text("UPDATE forecast_override_log SET undo_window_ends_at = NOW() - INTERVAL '1 minute' WHERE id = CAST(:id AS uuid)"),
        {"id": result.log_id},
    )
    with pytest.raises(F.UndoWindowExpired):
        F.undo_override(db, result.log_id)
