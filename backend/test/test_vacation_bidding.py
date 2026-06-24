"""
Unit tests for the pure vacation-bid award algorithm (compute_award).

These lock the design decisions: seniority waterfall, capacity exhaustion,
existing-leave exclusion + capacity netting, in-memory balance decrement across
multi-week wins, PTO-insufficiency skip, max-weeks cap, zero-win, denial trace.
No DB — compute_award is pure.
"""
from __future__ import annotations

from datetime import date

from app.services.vacation_bidding import (
    AgentSeniority,
    AwardInputs,
    WEEK_HOURS,
    compute_award,
)

W1 = date(2027, 1, 4)   # Mondays
W2 = date(2027, 1, 11)
W3 = date(2027, 1, 18)

SENIOR = AgentSeniority(1, "E1", "Senior", date(2010, 1, 1))
MID = AgentSeniority(2, "E2", "Mid", date(2015, 1, 1))
JUNIOR = AgentSeniority(3, "E3", "Junior", date(2020, 1, 1))


def _inputs(**over) -> AwardInputs:
    base = dict(
        max_weeks_per_agent=2,
        agents=[JUNIOR, SENIOR, MID],  # deliberately unsorted
        bids={},
        capacity={W1: 1, W2: 1, W3: 1},
        agent_off_weeks={},
        week_existing_off={},
        balances={1: 400.0, 2: 400.0, 3: 400.0},
    )
    base.update(over)
    return AwardInputs(**base)


def test_seniority_wins_contested_week() -> None:
    # All three want W1 first; only 1 slot. Most senior (SENIOR) wins it.
    inp = _inputs(bids={1: [(W1, 1)], 2: [(W1, 1)], 3: [(W1, 1)]})
    res = compute_award(inp)
    winners = {a["employee_id"]: a["week_start"] for a in res.awards}
    assert winners == {"E1": W1.isoformat()}
    # The other two are denied W1 as week_full.
    full = {(d["employee_id"], d["reason"]) for d in res.denials}
    assert ("E2", "week_full") in full and ("E3", "week_full") in full
    assert res.summary["n_zero_win"] == 2


def test_waterfall_falls_to_next_preference() -> None:
    # SENIOR takes W1; MID's 1st (W1) is full → MID gets 2nd choice W2.
    inp = _inputs(bids={2: [(W1, 1), (W2, 2)], 1: [(W1, 1)]})
    res = compute_award(inp)
    got = {a["employee_id"]: (a["week_start"], a["awarded_pref_rank"]) for a in res.awards}
    assert got["E1"] == (W1.isoformat(), 1)
    assert got["E2"] == (W2.isoformat(), 2)


def test_max_weeks_cap() -> None:
    inp = _inputs(max_weeks_per_agent=1, bids={1: [(W1, 1), (W2, 2)]})
    res = compute_award(inp)
    assert len([a for a in res.awards if a["employee_id"] == "E1"]) == 1


def test_in_memory_balance_decrement_blocks_second_week() -> None:
    # Balance covers exactly ONE week (50 >= 40, 10 < 40). The second bid must
    # be denied insufficient_pto — proves the balance decrements in-memory.
    inp = _inputs(
        max_weeks_per_agent=2,
        bids={1: [(W1, 1), (W2, 2)]},
        balances={1: 50.0, 2: 400.0, 3: 400.0},
    )
    res = compute_award(inp)
    e1_awards = [a for a in res.awards if a["employee_id"] == "E1"]
    assert len(e1_awards) == 1
    assert any(d["employee_id"] == "E1" and d["reason"] == "insufficient_pto" for d in res.denials)


def test_existing_leave_excluded_and_capacity_netted() -> None:
    # SENIOR already on leave W1 (excluded) AND that consumes the only W1 slot
    # (capacity netted), so MID bidding W1 is week_full, not awarded.
    inp = _inputs(
        bids={1: [(W1, 1)], 2: [(W1, 1)]},
        agent_off_weeks={1: {W1}},
        week_existing_off={W1: 1},
        capacity={W1: 1, W2: 1, W3: 1},
    )
    res = compute_award(inp)
    assert res.awards == []  # SENIOR already off (excluded), MID blocked by netted capacity
    reasons = {(d["employee_id"], d["reason"]) for d in res.denials}
    assert ("E1", "already_on_leave") in reasons
    assert ("E2", "week_full") in reasons


def test_insufficient_pto_skip() -> None:
    inp = _inputs(bids={3: [(W1, 1)]}, balances={1: 0, 2: 0, 3: 0.0})
    res = compute_award(inp)
    assert res.awards == []
    assert any(d["reason"] == "insufficient_pto" for d in res.denials)


def test_no_capacity_row_denial() -> None:
    other = date(2027, 2, 1)  # Monday, not in capacity
    inp = _inputs(bids={1: [(other, 1)]})
    res = compute_award(inp)
    assert res.awards == []
    assert any(d["reason"] == "no_capacity_defined" for d in res.denials)


def test_week_hours_constant() -> None:
    assert WEEK_HOURS == 40.0  # Mon–Fri × 8h, matches leave_pto_hours
