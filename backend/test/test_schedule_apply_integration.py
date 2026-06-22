"""
Postgres-backed integration tests for cherry-pick D (chat write actions).

The unit suite (`test_schedule_apply.py`) deliberately mocks the DB, so the
happy-path apply/undo — which is mostly SQL (JSONB snapshots, RETURNING id,
FK joins to agents/shift_segments, the optimistic-concurrency hash) — was
never actually exercised end-to-end. These tests close that gap by driving
the real services against a live Postgres.

They are SKIPPED automatically when no database is reachable, so they don't
break the mock-only CI box. To run locally:

    docker compose up -d postgres
    POSTGRES_USER=… POSTGRES_PASSWORD=… POSTGRES_DB=… \
      PYTHONPATH=. pytest test/test_schedule_apply_integration.py -v

Every test runs inside a transaction that is rolled back in teardown, so the
demo data in the container is left untouched.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services import notifications, summarize_change
from app.services.apply_tokens import consume_token, issue_token, mark_consumed
from app.services.schedule_change import (
    AlreadyUndone,
    StaleVersionError,
    UndoWindowExpired,
    apply_change,
    compute_schedule_version,
    undo_change,
)

# A date far enough out that it can't collide with any seeded schedule's range.
TEST_DATE = date(2099, 6, 1)
EMP_ID = "ITEST-AG-1"


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
    """A session wrapped in a transaction that is always rolled back.

    The services under test never commit (the router owns the commit), so
    rolling back here leaves zero residue in the container.
    """
    conn = engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, future=True)
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


def _dt(h: int, m: int = 0) -> datetime:
    return datetime.combine(TEST_DATE, datetime.min.time(), tzinfo=timezone.utc).replace(
        hour=h, minute=m
    )


@pytest.fixture
def fixture_schedule(db: Session) -> dict:
    """One agent, one schedule covering TEST_DATE, one 'available' segment
    12:00–13:00 (the lunchtime block the tests rewrite).
    """
    agent_id = db.execute(
        text(
            """
            INSERT INTO agents (employee_id, full_name, active)
            VALUES (:emp, 'Integration Test Agent', true)
            RETURNING id
            """
        ),
        {"emp": EMP_ID},
    ).scalar_one()

    schedule_id = db.execute(
        text(
            """
            INSERT INTO schedules (name, start_date, end_date, status, solver_status)
            VALUES ('itest schedule', :start, :end, 'complete', 'optimal')
            RETURNING id
            """
        ),
        {"start": TEST_DATE, "end": TEST_DATE},
    ).scalar_one()

    db.execute(
        text(
            """
            INSERT INTO shift_segments (schedule_id, agent_id, segment_type, start_time, end_time)
            VALUES (:sched, :aid, 'work', :start, :end)
            """
        ),
        {"sched": schedule_id, "aid": agent_id, "start": _dt(12, 0), "end": _dt(13, 0)},
    )
    return {"agent_id": agent_id, "schedule_id": schedule_id}


def _segments(db: Session, schedule_id: int, agent_id: int) -> list[tuple]:
    return db.execute(
        text(
            """
            SELECT segment_type, start_time, end_time
            FROM shift_segments
            WHERE schedule_id = :s AND agent_id = :a
            ORDER BY start_time
            """
        ),
        {"s": schedule_id, "a": agent_id},
    ).all()


# ---------------------------------------------------------------------------
# Happy path: preview → token → apply → audit log → notification → undo
# ---------------------------------------------------------------------------
def test_apply_then_undo_round_trips(db: Session, fixture_schedule: dict) -> None:
    schedule_id = fixture_schedule["schedule_id"]
    agent_id = fixture_schedule["agent_id"]

    # The change: convert the 12:00–13:00 work block into a lunch block.
    change_set = [
        {"agent_id": EMP_ID, "start": _dt(12, 0).isoformat(), "end": _dt(12, 30).isoformat(), "activity": "lunch"}
    ]
    version = compute_schedule_version(db, schedule_id, [EMP_ID], TEST_DATE)

    # Preview mints the token (single-use, pins schedule + version + change_set).
    issued = issue_token(db, schedule_id=schedule_id, schedule_version=version, change_set=change_set)
    consumed = consume_token(db, issued.token)
    assert consumed.schedule_id == schedule_id
    assert consumed.schedule_version == version
    assert consumed.consumed_log_id is None  # not yet applied

    # Apply.
    log_id = apply_change(
        db,
        schedule_id=schedule_id,
        expected_version=consumed.schedule_version,
        change_set=consumed.change_set,
        conversation_id=None,
        user_msg_id=None,
    )
    mark_consumed(db, issued.token, log_id)

    # The work block is gone; a lunch block now sits at 12:00–12:30.
    segs = _segments(db, schedule_id, agent_id)
    assert segs == [("lunch", _dt(12, 0), _dt(12, 30))]

    # Audit row carries non-null before/after snapshots + a 24h undo window.
    row = db.execute(
        text(
            """
            SELECT before_state, after_state, undo_window_ends_at, applied_at
            FROM schedule_change_log WHERE id = CAST(:id AS uuid)
            """
        ),
        {"id": log_id},
    ).mappings().one()
    assert row["before_state"] and row["after_state"]
    window = row["undo_window_ends_at"] - row["applied_at"]
    assert timedelta(hours=23, minutes=59) < window < timedelta(hours=24, minutes=1)

    # Deterministic server-side summary (no LLM).
    summary = summarize_change.summarize_change(row["before_state"], row["after_state"])
    assert "available" in summary and "lunch" in summary

    # A schedule_applied notification lands in the feed.
    notifications.notify_schedule_applied(
        db, summary=summary, log_id=log_id, schedule_id=schedule_id, conversation_id=None
    )
    feed, unread = notifications.list_notifications(db, limit=10)
    assert any(n["category"] == "schedule_applied" for n in feed)
    assert unread >= 1

    # Undo restores the original 12:00–13:00 work block.
    undo_log_id, _ = undo_change(db, log_id)
    assert undo_log_id != log_id
    assert _segments(db, schedule_id, agent_id) == [("work", _dt(12, 0), _dt(13, 0))]

    # Original row is marked undone, pointing at the undo row.
    undone = db.execute(
        text("SELECT undone_at, undone_by_log_id FROM schedule_change_log WHERE id = CAST(:id AS uuid)"),
        {"id": log_id},
    ).mappings().one()
    assert undone["undone_at"] is not None
    assert str(undone["undone_by_log_id"]) == undo_log_id


def test_idempotent_token_is_marked_consumed(db: Session, fixture_schedule: dict) -> None:
    """After apply, the same token reads back as consumed with the original
    log_id — the router maps that to a 200 no-op (decision D-6)."""
    schedule_id = fixture_schedule["schedule_id"]
    change_set = [
        {"agent_id": EMP_ID, "start": _dt(12, 0).isoformat(), "end": _dt(12, 30).isoformat(), "activity": "lunch"}
    ]
    version = compute_schedule_version(db, schedule_id, [EMP_ID], TEST_DATE)
    issued = issue_token(db, schedule_id=schedule_id, schedule_version=version, change_set=change_set)

    log_id = apply_change(
        db,
        schedule_id=schedule_id,
        expected_version=version,
        change_set=change_set,
        conversation_id=None,
        user_msg_id=None,
    )
    mark_consumed(db, issued.token, log_id)

    re_read = consume_token(db, issued.token)
    assert re_read.consumed_log_id == log_id


def test_stale_version_raises(db: Session, fixture_schedule: dict) -> None:
    """A concurrent external edit between preview and apply changes the version
    hash, so apply_change rejects the stale write (decision D-4 → 409)."""
    schedule_id = fixture_schedule["schedule_id"]
    agent_id = fixture_schedule["agent_id"]
    change_set = [
        {"agent_id": EMP_ID, "start": _dt(12, 0).isoformat(), "end": _dt(12, 30).isoformat(), "activity": "lunch"}
    ]
    stale_version = compute_schedule_version(db, schedule_id, [EMP_ID], TEST_DATE)

    # Simulate a manager editing the same window directly after the preview.
    db.execute(
        text(
            """
            UPDATE shift_segments SET end_time = :new_end
            WHERE schedule_id = :s AND agent_id = :a
            """
        ),
        {"new_end": _dt(13, 30), "s": schedule_id, "a": agent_id},
    )

    with pytest.raises(StaleVersionError) as exc:
        apply_change(
            db,
            schedule_id=schedule_id,
            expected_version=stale_version,
            change_set=change_set,
            conversation_id=None,
            user_msg_id=None,
        )
    assert exc.value.your_version == stale_version
    assert exc.value.current_version != stale_version


def test_undo_outside_window_raises(db: Session, fixture_schedule: dict) -> None:
    """A log row whose 24h window has already closed cannot be undone."""
    schedule_id = fixture_schedule["schedule_id"]
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    log_id = db.execute(
        text(
            """
            INSERT INTO schedule_change_log
                (applied_at, applied_by, schedule_id, change_set, before_state,
                 after_state, undo_window_ends_at)
            VALUES
                (:at, 'demo', :sched, CAST('[]' AS jsonb), CAST('[]' AS jsonb),
                 CAST('[]' AS jsonb), :window)
            RETURNING id
            """
        ),
        {"at": past - timedelta(hours=24), "sched": schedule_id, "window": past},
    ).scalar_one()

    with pytest.raises(UndoWindowExpired):
        undo_change(db, str(log_id))


def test_double_undo_raises_already_undone(db: Session, fixture_schedule: dict) -> None:
    schedule_id = fixture_schedule["schedule_id"]
    change_set = [
        {"agent_id": EMP_ID, "start": _dt(12, 0).isoformat(), "end": _dt(12, 30).isoformat(), "activity": "lunch"}
    ]
    version = compute_schedule_version(db, schedule_id, [EMP_ID], TEST_DATE)
    log_id = apply_change(
        db,
        schedule_id=schedule_id,
        expected_version=version,
        change_set=change_set,
        conversation_id=None,
        user_msg_id=None,
    )
    undo_change(db, log_id)
    with pytest.raises(AlreadyUndone):
        undo_change(db, log_id)
