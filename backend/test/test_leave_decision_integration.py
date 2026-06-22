"""
Postgres-backed integration tests for Surface #1 (leave-decision write actions).

Mirrors test_schedule_apply_integration.py: the mock-only unit suite
(test_leave_decision.py) can't exercise the SQL (JSONB snapshots, RETURNING id,
the pto_ledger round-trip, the optimistic-concurrency hash), so these drive the
real services against a live Postgres inside a rolled-back transaction.

SKIPPED automatically when no DB is reachable. To run:

    POSTGRES_USER=… POSTGRES_PASSWORD=… POSTGRES_DB=… \
      PYTHONPATH=. pytest test/test_leave_decision_integration.py -v
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services import notifications
from app.services.apply_tokens import (
    consume_leave_token,
    issue_leave_token,
    mark_leave_consumed,
)
from app.services.leave_decision import (
    AlreadyUndone,
    StaleVersionError,
    UndoWindowExpired,
    apply_decision,
    compute_request_version,
    undo_decision,
)

EMP_ID = "ITEST-LEAVE-1"
# Far-future window so it can't collide with seeded staffing/leave data.
START = datetime(2099, 6, 1, 9, 0, tzinfo=timezone.utc)
END = datetime(2099, 6, 3, 17, 0, tzinfo=timezone.utc)  # 3 calendar days = 24h PTO
INIT_BALANCE = 80.0
EXPECTED_PTO = 24.0


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
def fixture_leave(db: Session) -> dict:
    agent_id = db.execute(
        text(
            """
            INSERT INTO agents (employee_id, full_name, active)
            VALUES (:emp, 'Integration Leave Agent', true)
            RETURNING id
            """
        ),
        {"emp": EMP_ID},
    ).scalar_one()
    # Stamp the accrual on the sim clock (like seed data) and before sim_now, so
    # the decision's 'use' row — written at sim_now() — is the latest ledger row.
    db.execute(
        text(
            """
            INSERT INTO pto_ledger (agent_id, event_ts, event_type, hours, balance_after, note)
            VALUES (:aid, sim_now() - INTERVAL '30 days', 'accrual', :h, :h, 'itest init')
            """
        ),
        {"aid": agent_id, "h": INIT_BALANCE},
    )
    request_id = db.execute(
        text(
            """
            INSERT INTO leave_requests (agent_id, start_ts, end_ts, leave_type, status)
            VALUES (:aid, :start, :end, 'PTO', 'pending')
            RETURNING id
            """
        ),
        {"aid": agent_id, "start": START, "end": END},
    ).scalar_one()
    return {"agent_id": agent_id, "request_id": request_id}


def _balance(db: Session, agent_id: int) -> float:
    return float(
        db.execute(
            text(
                "SELECT balance_after FROM pto_ledger WHERE agent_id=:a "
                "ORDER BY event_ts DESC, id DESC LIMIT 1"
            ),
            {"a": agent_id},
        ).scalar()
    )


# ---------------------------------------------------------------------------
# Happy path: preview token → apply → ledger → audit → notification → undo
# ---------------------------------------------------------------------------
def test_approve_then_undo_round_trips(db: Session, fixture_leave: dict) -> None:
    rid = fixture_leave["request_id"]
    agent_id = fixture_leave["agent_id"]
    version = compute_request_version("pending", None)

    issued = issue_leave_token(
        db, request_id=rid, request_version=version, decision="approve", note="ok"
    )
    consumed = consume_leave_token(db, issued.token)
    assert consumed.request_id == rid
    assert consumed.decision == "approve"
    assert consumed.consumed_log_id is None

    result = apply_decision(
        db,
        request_id=rid,
        expected_version=consumed.request_version,
        decision=consumed.decision,
        note=consumed.note,
        conversation_id=None,
    )
    mark_leave_consumed(db, issued.token, result.log_id)

    # Request flipped + decision fields set.
    row = db.execute(
        text("SELECT status, decided_by, decision_note FROM leave_requests WHERE id=:i"),
        {"i": rid},
    ).mappings().one()
    assert row["status"] == "approved"
    assert row["decided_by"] == "demo"
    assert row["decision_note"] == "ok"

    # PTO consumed.
    assert _balance(db, agent_id) == pytest.approx(INIT_BALANCE - EXPECTED_PTO)
    use_row = db.execute(
        text("SELECT event_type, hours FROM pto_ledger WHERE id=:i"),
        {"i": result.ledger_event_id},
    ).mappings().one()
    assert use_row["event_type"] == "use"
    assert float(use_row["hours"]) == pytest.approx(-EXPECTED_PTO)

    # Audit row carries snapshots + a 24h window.
    log = db.execute(
        text(
            "SELECT before_state, after_state, undo_window_ends_at, applied_at "
            "FROM leave_decision_log WHERE id = CAST(:id AS uuid)"
        ),
        {"id": result.log_id},
    ).mappings().one()
    assert log["before_state"]["status"] == "pending"
    assert log["after_state"]["status"] == "approved"
    window = log["undo_window_ends_at"] - log["applied_at"]
    assert timedelta(hours=23, minutes=59) < window < timedelta(hours=24, minutes=1)

    # Notification lands.
    notifications.notify_leave_decided(
        db,
        summary=result.summary,
        log_id=result.log_id,
        request_id=rid,
        decision="approve",
        conversation_id=None,
    )
    feed, unread = notifications.list_notifications(db, limit=10)
    assert any(n["category"] == "leave_decided" for n in feed)
    assert unread >= 1

    # Undo restores status + balance, marks the row undone.
    undo = undo_decision(db, result.log_id)
    assert undo.request_id == rid
    restored = db.execute(
        text("SELECT status, decided_at, decided_by FROM leave_requests WHERE id=:i"),
        {"i": rid},
    ).mappings().one()
    assert restored["status"] == "pending"
    assert restored["decided_at"] is None
    assert restored["decided_by"] is None
    assert _balance(db, agent_id) == pytest.approx(INIT_BALANCE)
    undone = db.execute(
        text("SELECT undone_at FROM leave_decision_log WHERE id = CAST(:id AS uuid)"),
        {"id": result.log_id},
    ).scalar()
    assert undone is not None


def test_deny_writes_no_ledger_row(db: Session, fixture_leave: dict) -> None:
    rid = fixture_leave["request_id"]
    agent_id = fixture_leave["agent_id"]
    version = compute_request_version("pending", None)
    result = apply_decision(
        db,
        request_id=rid,
        expected_version=version,
        decision="deny",
        note="blackout",
        conversation_id=None,
    )
    assert result.status == "denied"
    assert result.ledger_event_id is None
    assert _balance(db, agent_id) == pytest.approx(INIT_BALANCE)  # untouched
    status = db.execute(
        text("SELECT status FROM leave_requests WHERE id=:i"), {"i": rid}
    ).scalar()
    assert status == "denied"


def test_idempotent_token_marked_consumed(db: Session, fixture_leave: dict) -> None:
    rid = fixture_leave["request_id"]
    version = compute_request_version("pending", None)
    issued = issue_leave_token(
        db, request_id=rid, request_version=version, decision="approve"
    )
    result = apply_decision(
        db,
        request_id=rid,
        expected_version=version,
        decision="approve",
        note=None,
        conversation_id=None,
    )
    mark_leave_consumed(db, issued.token, result.log_id)
    re_read = consume_leave_token(db, issued.token)
    assert re_read.consumed_log_id == result.log_id


def test_stale_version_raises(db: Session, fixture_leave: dict) -> None:
    rid = fixture_leave["request_id"]
    stale_version = compute_request_version("pending", None)
    # Simulate a manager deciding the same request between preview and apply.
    db.execute(
        text("UPDATE leave_requests SET status='denied', decided_at=NOW() WHERE id=:i"),
        {"i": rid},
    )
    with pytest.raises(StaleVersionError) as exc:
        apply_decision(
            db,
            request_id=rid,
            expected_version=stale_version,
            decision="approve",
            note=None,
            conversation_id=None,
        )
    assert exc.value.your_version == stale_version
    assert exc.value.current_version != stale_version


def test_undo_outside_window_raises(db: Session, fixture_leave: dict) -> None:
    rid = fixture_leave["request_id"]
    version = compute_request_version("pending", None)
    result = apply_decision(
        db,
        request_id=rid,
        expected_version=version,
        decision="approve",
        note=None,
        conversation_id=None,
    )
    db.execute(
        text(
            "UPDATE leave_decision_log SET undo_window_ends_at = NOW() - INTERVAL '1 minute' "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"id": result.log_id},
    )
    with pytest.raises(UndoWindowExpired):
        undo_decision(db, result.log_id)


def test_double_undo_raises(db: Session, fixture_leave: dict) -> None:
    rid = fixture_leave["request_id"]
    version = compute_request_version("pending", None)
    result = apply_decision(
        db,
        request_id=rid,
        expected_version=version,
        decision="approve",
        note=None,
        conversation_id=None,
    )
    undo_decision(db, result.log_id)
    with pytest.raises(AlreadyUndone):
        undo_decision(db, result.log_id)
