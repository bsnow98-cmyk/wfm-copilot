"""
Postgres-backed integration tests for Surface #2 (offer publish/retract).

SKIPPED automatically when no DB is reachable. Runs inside a rolled-back
transaction so the container's demo data is left untouched.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services import notifications, offer
from app.services.apply_tokens import (
    consume_offer_token,
    issue_offer_token,
    mark_offer_consumed,
)


def _spec() -> dict:
    return {
        "kind": "ot",
        "schedule_id": None,
        "target_date": "2099-06-01",
        "window_start": "2099-06-01T09:00:00+00:00",
        "window_end": "2099-06-01T12:00:00+00:00",
        "targets": [
            {"employee_id": "X1", "full_name": "Test One"},
            {"employee_id": "X2", "full_name": "Test Two"},
        ],
        "slots": 2,
        "policy": "seniority_desc",
        "message": "coverage",
    }


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


def test_publish_then_retract_round_trips(db: Session) -> None:
    issued = issue_offer_token(db, spec=_spec())
    consumed = consume_offer_token(db, issued.token)
    assert consumed.spec["kind"] == "ot"
    assert consumed.consumed_offer_id is None

    result = offer.publish_offer(db, spec=consumed.spec, conversation_id=None)
    mark_offer_consumed(db, issued.token, result.offer_id)
    assert result.n_targets == 2 and result.slots == 2

    row = db.execute(
        text(
            "SELECT kind, status, slots, jsonb_array_length(targets) AS n, "
            "undo_window_ends_at, published_at FROM offers WHERE id=:i"
        ),
        {"i": result.offer_id},
    ).mappings().one()
    assert row["kind"] == "ot" and row["status"] == "open" and row["n"] == 2
    window = row["undo_window_ends_at"] - row["published_at"]
    assert timedelta(hours=23, minutes=59) < window < timedelta(hours=24, minutes=1)

    # Idempotent re-consume.
    assert consume_offer_token(db, issued.token).consumed_offer_id == result.offer_id

    # Notification lands.
    notifications.notify_offer_published(
        db, summary=result.summary, offer_id=result.offer_id, kind="ot", conversation_id=None
    )
    feed, unread = notifications.list_notifications(db, limit=10)
    assert any(n["category"] == "offer_published" for n in feed)
    assert unread >= 1

    # Retract.
    r = offer.retract_offer(db, result.offer_id)
    assert r.offer_id == result.offer_id
    st = db.execute(
        text("SELECT status, retracted_at FROM offers WHERE id=:i"), {"i": result.offer_id}
    ).mappings().one()
    assert st["status"] == "retracted" and st["retracted_at"] is not None


def test_retract_outside_window_raises(db: Session) -> None:
    issued = issue_offer_token(db, spec=_spec())
    consumed = consume_offer_token(db, issued.token)
    result = offer.publish_offer(db, spec=consumed.spec, conversation_id=None)
    db.execute(
        text("UPDATE offers SET undo_window_ends_at = NOW() - INTERVAL '1 minute' WHERE id=:i"),
        {"i": result.offer_id},
    )
    with pytest.raises(offer.RetractWindowExpired):
        offer.retract_offer(db, result.offer_id)


def test_double_retract_raises(db: Session) -> None:
    issued = issue_offer_token(db, spec=_spec())
    consumed = consume_offer_token(db, issued.token)
    result = offer.publish_offer(db, spec=consumed.spec, conversation_id=None)
    offer.retract_offer(db, result.offer_id)
    with pytest.raises(offer.AlreadyRetracted):
        offer.retract_offer(db, result.offer_id)
