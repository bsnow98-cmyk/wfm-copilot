"""
vacation_bidding — seniority-greedy weekly vacation bid award.

Design: docs/designs/VACATION_BIDDING.md. The award is Surface #1 (leave
approval) batched: it writes approved leave_requests + pto_ledger holds in one
transaction. `compute_award` is a PURE function (no DB) so the waterfall is
unit-testable; DB I/O lives in load/apply/undo.

Locked decisions baked in here:
- Award window = Mon–Fri (5 workdays → leave_pto_hours = 40h, matches the gate).
- Existing approved/pending leave is excluded per-agent AND netted out of per-week
  capacity (no double-book, no over-capacity).
- Balance decrements in-memory across an agent's multi-week wins.
- Every non-award is recorded with a reason (durable denial trace).
- Concurrency guard pins (existing-leave + capacity) version, not the frozen bids.
- Award commits silently; notification is a separate publish step.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.realtime_clock import sim_now

WEEK_HOURS = 40.0          # Mon–Fri × 8h
UNDO_WINDOW = timedelta(hours=24)


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------
class RoundNotFound(Exception):
    pass


class RoundNotClosed(Exception):
    """Award/preview requires the round in 'closed' state (bids frozen)."""


class StaleInputsError(Exception):
    def __init__(self, your_version: int, current_version: int, round_id: int | None = None) -> None:
        super().__init__(
            f"bid inputs changed (yours={your_version}, current={current_version})"
        )
        self.your_version = your_version
        self.current_version = current_version
        self.round_id = round_id


class AwardNotFound(Exception):
    pass


class AlreadyUndone(Exception):
    pass


class UndoWindowExpired(Exception):
    pass


class AlreadyPublished(Exception):
    """Undo is blocked once agents have been notified."""


# --------------------------------------------------------------------------
# Pure data + algorithm
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AgentSeniority:
    agent_id: int
    employee_id: str
    full_name: str
    hire_date: date | None


@dataclass
class AwardInputs:
    max_weeks_per_agent: int
    agents: list[AgentSeniority]                 # any order; sorted inside compute
    bids: dict[int, list[tuple[date, int]]]      # agent_id -> [(week_start, pref_rank)]
    capacity: dict[date, int]                    # week_start -> slots
    agent_off_weeks: dict[int, set[date]]        # agent_id -> weeks already on leave
    week_existing_off: dict[date, int]           # week_start -> count of agents already off
    balances: dict[int, float]                   # agent_id -> PTO balance (hours)


@dataclass
class AwardResult:
    awards: list[dict[str, Any]] = field(default_factory=list)
    denials: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def _seniority_key(a: AgentSeniority) -> tuple:
    # Most senior first: earliest hire_date. None → least senior. Tie-break:
    # employee_id (deterministic placeholder; real policy is a v2 hook).
    return (a.hire_date or date.max, a.employee_id)


def compute_award(inp: AwardInputs) -> AwardResult:
    """Pure seniority-greedy waterfall. No DB, no clock."""
    ordered = sorted(inp.agents, key=_seniority_key)
    seniority_rank = {a.agent_id: i + 1 for i, a in enumerate(ordered)}

    remaining = {w: slots - inp.week_existing_off.get(w, 0) for w, slots in inp.capacity.items()}
    res = AwardResult()
    agents_with_award: set[int] = set()
    zero_win = 0

    for a in ordered:
        awarded = 0
        bal = inp.balances.get(a.agent_id, 0.0)
        had_bid = bool(inp.bids.get(a.agent_id))
        for week, pref_rank in inp.bids.get(a.agent_id, []):
            if awarded >= inp.max_weeks_per_agent:
                break
            base = {
                "agent_id": a.agent_id,
                "employee_id": a.employee_id,
                "seniority_rank": seniority_rank[a.agent_id],
                "week_start": week.isoformat(),
                "pref_rank": pref_rank,
            }
            if week not in remaining:
                res.denials.append({**base, "reason": "no_capacity_defined"})
                continue
            if week in inp.agent_off_weeks.get(a.agent_id, set()):
                res.denials.append({**base, "reason": "already_on_leave"})
                continue
            if remaining[week] <= 0:
                res.denials.append({**base, "reason": "week_full"})
                continue
            if bal < WEEK_HOURS:
                res.denials.append({**base, "reason": "insufficient_pto"})
                continue
            res.awards.append({
                **{k: base[k] for k in ("agent_id", "employee_id", "seniority_rank", "week_start")},
                "full_name": a.full_name,
                "awarded_pref_rank": pref_rank,
            })
            remaining[week] -= 1
            awarded += 1
            bal -= WEEK_HOURS
            agents_with_award.add(a.agent_id)
        if awarded == 0 and had_bid:
            zero_win += 1

    res.summary = {
        "n_awarded": len(res.awards),
        "n_agents": len(agents_with_award),
        "n_zero_win": zero_win,
        "weeks_at_capacity": sorted(w.isoformat() for w, r in remaining.items() if r <= 0),
    }
    return res


# --------------------------------------------------------------------------
# Loading + versioning (DB)
# --------------------------------------------------------------------------
def load_round(db: Session, round_id: int) -> dict[str, Any] | None:
    row = (
        db.execute(
            text(
                """
                SELECT id, name, status, season_start, season_end, max_weeks_per_agent,
                       awarded_at, published_at
                FROM bid_rounds WHERE id = :id
                """
            ),
            {"id": round_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _monday_window(week_start: date) -> tuple[datetime, datetime]:
    """Mon 09:00 → Fri 17:00 UTC. leave_pto_hours = (Fri-Mon).days+1 = 5 → 40h."""
    start = datetime.combine(week_start, time(9, 0), tzinfo=timezone.utc)
    end = datetime.combine(week_start + timedelta(days=4), time(17, 0), tzinfo=timezone.utc)
    return start, end


def load_inputs(db: Session, round_id: int) -> AwardInputs:
    """Pull everything compute_award needs. Two aggregate queries for the
    movable inputs (leave + capacity); no per-agent/per-week N+1."""
    rnd = load_round(db, round_id)
    if rnd is None:
        raise RoundNotFound(f"bid round {round_id} not found")
    season_start: date = rnd["season_start"]
    season_end: date = rnd["season_end"]
    win_lo = datetime.combine(season_start, time.min, tzinfo=timezone.utc)
    win_hi = datetime.combine(season_end + timedelta(days=7), time.min, tzinfo=timezone.utc)

    agents = [
        AgentSeniority(int(r["id"]), r["employee_id"], r["full_name"], r["hire_date"])
        for r in db.execute(
            text("SELECT id, employee_id, full_name, hire_date FROM agents WHERE active = TRUE")
        ).mappings().all()
    ]

    bids: dict[int, list[tuple[date, int]]] = {}
    for r in db.execute(
        text(
            "SELECT agent_id, week_start, rank FROM vacation_bids "
            "WHERE round_id = :r ORDER BY agent_id, rank"
        ),
        {"r": round_id},
    ).mappings().all():
        bids.setdefault(int(r["agent_id"]), []).append((r["week_start"], int(r["rank"])))

    capacity = {
        r["week_start"]: int(r["slots"])
        for r in db.execute(
            text("SELECT week_start, slots FROM bid_week_capacity WHERE round_id = :r"),
            {"r": round_id},
        ).mappings().all()
    }

    # Existing approved/pending leave overlapping the season → exclusion + capacity net.
    agent_off_weeks: dict[int, set[date]] = {}
    week_existing_off: dict[date, int] = {}
    weeks = list(capacity.keys())
    leave_rows = db.execute(
        text(
            """
            SELECT agent_id, start_ts, end_ts FROM leave_requests
            WHERE status IN ('approved','pending')
              AND start_ts < :hi AND end_ts > :lo
            """
        ),
        {"lo": win_lo, "hi": win_hi},
    ).mappings().all()
    for lr in leave_rows:
        aid = int(lr["agent_id"])
        for w in weeks:
            ws, we = _monday_window(w)
            if lr["start_ts"] < we and lr["end_ts"] > ws:  # overlaps this week
                agent_off_weeks.setdefault(aid, set()).add(w)
                week_existing_off[w] = week_existing_off.get(w, 0) + 1

    balances = {
        int(r["agent_id"]): float(r["bal"] or 0.0)
        for r in db.execute(
            text(
                """
                SELECT DISTINCT ON (agent_id) agent_id, balance_after AS bal
                FROM pto_ledger ORDER BY agent_id, event_ts DESC, id DESC
                """
            )
        ).mappings().all()
    }

    return AwardInputs(
        max_weeks_per_agent=int(rnd["max_weeks_per_agent"]),
        agents=agents,
        bids=bids,
        capacity=capacity,
        agent_off_weeks=agent_off_weeks,
        week_existing_off=week_existing_off,
        balances=balances,
    )


def compute_inputs_version(inp: AwardInputs) -> int:
    """Hash the movable inputs (capacity + existing-leave), NOT the frozen bids —
    those are what can drift between preview and apply while the round is closed."""
    cap = "|".join(f"{w.isoformat()}={s}" for w, s in sorted(inp.capacity.items()))
    off = "|".join(
        f"{w.isoformat()}={inp.week_existing_off.get(w, 0)}" for w in sorted(inp.capacity)
    )
    digest = hashlib.sha256(f"{cap}#{off}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


# --------------------------------------------------------------------------
# Apply (batch, all-or-nothing) — silent (no notify; publish is separate)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class AwardApplyResult:
    log_id: str
    round_id: int
    n_awarded: int
    n_zero_win: int
    applied_at: datetime
    summary: str


def apply_award(
    db: Session,
    *,
    round_id: int,
    expected_version: int,
    conversation_id: str | None,
    actor: str = "demo",
) -> AwardApplyResult:
    """Guard → recompute → batch-write approved leave + PTO holds + audit →
    flip round to 'awarded'. One transaction; caller commits. Does NOT notify."""
    rnd = load_round(db, round_id)
    if rnd is None:
        raise RoundNotFound(f"bid round {round_id} not found")
    if rnd["status"] != "closed":
        raise RoundNotClosed(f"round {round_id} is '{rnd['status']}', must be 'closed' to award")

    # Lock the round row so two awards can't race.
    db.execute(text("SELECT id FROM bid_rounds WHERE id = :id FOR UPDATE"), {"id": round_id})

    inp = load_inputs(db, round_id)
    current_version = compute_inputs_version(inp)
    if current_version != expected_version:
        raise StaleInputsError(expected_version, current_version, round_id=round_id)

    result = compute_award(inp)
    decided_at = sim_now(db)

    # Running balance per agent so multi-week holds chain correctly.
    running = dict(inp.balances)
    awards_json: list[dict[str, Any]] = []
    for aw in result.awards:
        aid = int(aw["agent_id"])
        week = date.fromisoformat(aw["week_start"])
        start_ts, end_ts = _monday_window(week)
        leave_id = db.execute(
            text(
                """
                INSERT INTO leave_requests
                    (agent_id, start_ts, end_ts, leave_type, status,
                     reason, decided_at, decided_by, decision_note)
                VALUES
                    (:aid, :start, :end, 'PTO', 'approved',
                     :reason, :at, :actor, :note)
                RETURNING id
                """
            ),
            {
                "aid": aid, "start": start_ts, "end": end_ts,
                "reason": f"Vacation bid award (round {round_id})",
                "at": decided_at, "actor": actor,
                "note": f"Awarded week {aw['week_start']} — bid round {round_id}, pref #{aw['awarded_pref_rank']}",
            },
        ).scalar_one()

        bal = running.get(aid, 0.0) - WEEK_HOURS
        running[aid] = bal
        ledger_id = db.execute(
            text(
                """
                INSERT INTO pto_ledger (agent_id, event_ts, event_type, hours, balance_after, note)
                VALUES (:aid, :at, 'use', :hours, :bal, :note)
                RETURNING id
                """
            ),
            {
                "aid": aid, "at": decided_at, "hours": -WEEK_HOURS, "bal": bal,
                "note": f"Vacation bid award (round {round_id}, week {aw['week_start']})",
            },
        ).scalar_one()

        awards_json.append({**aw, "leave_request_id": int(leave_id), "ledger_event_id": int(ledger_id)})

    applied_at = datetime.now(timezone.utc)
    log_id = db.execute(
        text(
            """
            INSERT INTO vacation_award_log
                (round_id, applied_at, applied_by, conversation_id,
                 awards, denials, summary, undo_window_ends_at)
            VALUES
                (:rid, :at, :actor, CAST(:conv AS uuid),
                 CAST(:awards AS jsonb), CAST(:denials AS jsonb), CAST(:summary AS jsonb), :undo_until)
            RETURNING id
            """
        ),
        {
            "rid": round_id, "at": applied_at, "actor": actor, "conv": conversation_id,
            "awards": json.dumps(awards_json, default=str),
            "denials": json.dumps(result.denials, default=str),
            "summary": json.dumps(result.summary, default=str),
            "undo_until": applied_at + UNDO_WINDOW,
        },
    ).scalar_one()

    db.execute(
        text("UPDATE bid_rounds SET status='awarded', awarded_at=:at WHERE id=:id"),
        {"at": applied_at, "id": round_id},
    )

    summary = (
        f"Awarded {result.summary['n_awarded']} weeks to {result.summary['n_agents']} agents "
        f"(round {round_id}); {result.summary['n_zero_win']} got nothing"
    )
    return AwardApplyResult(
        log_id=str(log_id),
        round_id=round_id,
        n_awarded=result.summary["n_awarded"],
        n_zero_win=result.summary["n_zero_win"],
        applied_at=applied_at,
        summary=summary,
    )


# --------------------------------------------------------------------------
# Undo (strict reverse + drift report)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class UndoResult:
    log_id: str
    round_id: int
    reversed_count: int
    drifted: list[dict[str, Any]]
    undone_at: datetime
    summary: str


def undo_award(db: Session, log_id: str) -> UndoResult:
    row = (
        db.execute(
            text(
                """
                SELECT id, round_id, awards, undo_window_ends_at, undone_at
                FROM vacation_award_log WHERE id = CAST(:id AS uuid) FOR UPDATE
                """
            ),
            {"id": log_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise AwardNotFound(f"vacation_award_log {log_id} not found")
    if row["undone_at"] is not None:
        raise AlreadyUndone(f"award {log_id} already undone")
    if row["undo_window_ends_at"] < datetime.now(timezone.utc):
        raise UndoWindowExpired(f"award {log_id} past the 24h undo window")

    round_id = int(row["round_id"])
    rnd = load_round(db, round_id)
    if rnd and rnd["published_at"] is not None:
        raise AlreadyPublished(f"round {round_id} already published — undo blocked")

    reversed_count = 0
    drifted: list[dict[str, Any]] = []
    for aw in row["awards"] or []:
        lr_id = aw.get("leave_request_id")
        ledger_id = aw.get("ledger_event_id")
        # Only reverse a leave row still in the exact state the award created.
        lr = db.execute(
            text(
                "SELECT status, leave_type FROM leave_requests WHERE id = :id FOR UPDATE"
            ),
            {"id": lr_id},
        ).mappings().first()
        if lr is None or lr["status"] != "approved" or lr["leave_type"] != "PTO":
            drifted.append({"leave_request_id": lr_id, "week_start": aw.get("week_start"),
                            "reason": "row missing or changed since award"})
            continue
        db.execute(text("DELETE FROM leave_requests WHERE id = :id"), {"id": lr_id})

        # Reverse the hold with a compensating adjust (append-only ledger), only
        # if it hasn't already been compensated.
        used = db.execute(
            text("SELECT agent_id, hours FROM pto_ledger WHERE id = :id"),
            {"id": ledger_id},
        ).mappings().first()
        if used is not None:
            aid = int(used["agent_id"])
            give_back = -float(used["hours"])
            at = sim_now(db)
            prior = float(
                db.execute(
                    text(
                        "SELECT balance_after FROM pto_ledger WHERE agent_id=:a "
                        "ORDER BY event_ts DESC, id DESC LIMIT 1"
                    ),
                    {"a": aid},
                ).scalar() or 0.0
            )
            db.execute(
                text(
                    """
                    INSERT INTO pto_ledger (agent_id, event_ts, event_type, hours, balance_after, note)
                    VALUES (:a, :at, 'adjust', :h, :bal, :note)
                    """
                ),
                {"a": aid, "at": at, "h": give_back, "bal": prior + give_back,
                 "note": f"Reversed vacation award (undo {log_id})"},
            )
        reversed_count += 1

    undone_at = datetime.now(timezone.utc)
    db.execute(
        text("UPDATE vacation_award_log SET undone_at=:at WHERE id=CAST(:id AS uuid)"),
        {"at": undone_at, "id": log_id},
    )
    # Reopen the round so it can be re-awarded.
    db.execute(
        text("UPDATE bid_rounds SET status='closed', awarded_at=NULL WHERE id=:id"),
        {"id": round_id},
    )
    summary = f"Undid vacation award (round {round_id}): reversed {reversed_count}, {len(drifted)} drifted"
    return UndoResult(
        log_id=str(row["id"]), round_id=round_id, reversed_count=reversed_count,
        drifted=drifted, undone_at=undone_at, summary=summary,
    )


def publish_round(db: Session, round_id: int) -> dict[str, Any]:
    """Mark the round published (notification trigger). Separate from award so
    the manager can review/undo before agents are told."""
    rnd = load_round(db, round_id)
    if rnd is None:
        raise RoundNotFound(f"bid round {round_id} not found")
    if rnd["status"] != "awarded":
        raise RoundNotClosed(f"round {round_id} is '{rnd['status']}', must be 'awarded' to publish")
    published_at = datetime.now(timezone.utc)
    db.execute(
        text("UPDATE bid_rounds SET published_at=:at WHERE id=:id"),
        {"at": published_at, "id": round_id},
    )
    return {"round_id": round_id, "published_at": published_at}
