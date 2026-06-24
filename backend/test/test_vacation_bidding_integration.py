"""
Postgres-backed integration tests for the vacation-bid award batch write.

Covers what the pure unit tests can't: the batch leave + PTO ledger writes, the
stale-inputs 409 guard, and undo's strict drift handling. Rolled back; SKIPPED
when no DB is reachable.
"""
from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services import vacation_bidding as V

W1, W2 = date(2098, 1, 6), date(2098, 1, 13)  # far-future Mondays


def _database_url() -> str | None:
    u, p, d = (os.environ.get(k) for k in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"))
    if not (u and p and d):
        return None
    host = os.environ.get("TEST_POSTGRES_HOST", "localhost")
    return f"postgresql+psycopg://{u}:{p}@{host}:{os.environ.get('TEST_POSTGRES_PORT','5432')}/{d}"


@pytest.fixture(scope="module")
def engine():
    url = _database_url()
    if url is None:
        pytest.skip("POSTGRES_* not set")
    eng = create_engine(url, future=True)
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable ({type(exc).__name__})")
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    conn = engine.connect()
    trans = conn.begin()
    s = Session(bind=conn, future=True)
    try:
        yield s
    finally:
        s.close()
        trans.rollback()
        conn.close()


@pytest.fixture
def round_fixture(db: Session) -> dict:
    a1 = db.execute(text("INSERT INTO agents (employee_id,full_name,active,hire_date) VALUES ('VAC1','Sr',true,'2010-01-01') RETURNING id")).scalar_one()
    a2 = db.execute(text("INSERT INTO agents (employee_id,full_name,active,hire_date) VALUES ('VAC2','Jr',true,'2020-01-01') RETURNING id")).scalar_one()
    for a in (a1, a2):
        db.execute(text("INSERT INTO pto_ledger (agent_id,event_ts,event_type,hours,balance_after,note) VALUES (:a, sim_now()-INTERVAL '30 days','accrual',120,120,'init')"), {"a": a})
    rid = db.execute(text("""INSERT INTO bid_rounds (name,status,bids_open_at,bids_close_at,season_start,season_end,max_weeks_per_agent)
        VALUES ('itest','closed',NOW(),NOW(),:s,:e,2) RETURNING id"""), {"s": W1, "e": W2}).scalar_one()
    db.execute(text("INSERT INTO bid_week_capacity (round_id,week_start,slots) VALUES (:r,:w,1),(:r,:w2,2)"), {"r": rid, "w": W1, "w2": W2})
    db.execute(text("INSERT INTO vacation_bids (round_id,agent_id,week_start,rank) VALUES (:r,:a,:w,1)"), {"r": rid, "a": a1, "w": W1})
    db.execute(text("INSERT INTO vacation_bids (round_id,agent_id,week_start,rank) VALUES (:r,:a,:w,1),(:r,:a,:w2,2)"), {"r": rid, "a": a2, "w": W1, "w2": W2})
    return {"round_id": int(rid), "a1": int(a1), "a2": int(a2)}


def test_apply_then_undo_round_trips(db: Session, round_fixture: dict) -> None:
    rid = round_fixture["round_id"]
    inp = V.load_inputs(db, rid)
    ver = V.compute_inputs_version(inp)
    res = V.apply_award(db, round_id=rid, expected_version=ver, conversation_id=None, actor="jchen")
    assert res.n_awarded == 2  # Sr→W1, Jr→W2 (Jr's W1 was full)
    assert db.execute(text("SELECT count(*) FROM leave_requests WHERE decision_note LIKE :p"), {"p": f"%bid round {rid}%"}).scalar() == 2
    assert V.load_round(db, rid)["status"] == "awarded"
    # denial trace persisted (Jr denied W1 week_full)
    den = db.execute(text("SELECT denials FROM vacation_award_log WHERE id=CAST(:i AS uuid)"), {"i": res.log_id}).scalar()
    assert any(d["reason"] == "week_full" for d in den)

    undo = V.undo_award(db, res.log_id)
    assert undo.reversed_count == 2 and undo.drifted == []
    assert db.execute(text("SELECT count(*) FROM leave_requests WHERE decision_note LIKE :p"), {"p": f"%bid round {rid}%"}).scalar() == 0
    assert V.load_round(db, rid)["status"] == "closed"


def test_stale_inputs_409(db: Session, round_fixture: dict) -> None:
    rid = round_fixture["round_id"]
    inp = V.load_inputs(db, rid)
    stale = V.compute_inputs_version(inp)
    # Capacity change between preview and apply moves the input version.
    db.execute(text("UPDATE bid_week_capacity SET slots=5 WHERE round_id=:r AND week_start=:w"), {"r": rid, "w": W1})
    with pytest.raises(V.StaleInputsError) as exc:
        V.apply_award(db, round_id=rid, expected_version=stale, conversation_id=None, actor="jchen")
    assert exc.value.round_id == rid


def test_undo_reports_drift(db: Session, round_fixture: dict) -> None:
    rid = round_fixture["round_id"]
    inp = V.load_inputs(db, rid)
    res = V.apply_award(db, round_id=rid, expected_version=V.compute_inputs_version(inp), conversation_id=None, actor="jchen")
    # Simulate an agent cancelling one awarded week before undo.
    awards = db.execute(text("SELECT awards FROM vacation_award_log WHERE id=CAST(:i AS uuid)"), {"i": res.log_id}).scalar()
    drifted_lr = awards[0]["leave_request_id"]
    db.execute(text("UPDATE leave_requests SET status='cancelled' WHERE id=:i"), {"i": drifted_lr})
    undo = V.undo_award(db, res.log_id)
    assert len(undo.drifted) == 1 and undo.drifted[0]["leave_request_id"] == drifted_lr
    assert undo.reversed_count == res.n_awarded - 1


def test_award_requires_closed(db: Session, round_fixture: dict) -> None:
    rid = round_fixture["round_id"]
    db.execute(text("UPDATE bid_rounds SET status='open' WHERE id=:r"), {"r": rid})
    inp = V.load_inputs(db, rid)
    with pytest.raises(V.RoundNotClosed):
        V.apply_award(db, round_id=rid, expected_version=V.compute_inputs_version(inp), conversation_id=None, actor="jchen")
