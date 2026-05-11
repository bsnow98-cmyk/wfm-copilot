"""
Generate synthetic Wave 3+4 data on top of an already-seeded WFM Copilot DB.

Reads existing agents, skills, and shift_segments and produces:

- agent_aux_events       — strict adherence actuals (continuous per-shift)
- adherence_exceptions   — deviations explained (late_start, missed_break, ...)
- pto_ledger             — accruals + uses going back ~12 months
- leave_requests         — pending + decided around sim-now
- training_events        — team meetings, coaching, new-hire class
- training_attendees     — who's on each event
- agent_certifications   — derived from agent_skills proficiencies
- agent_qa_scores        — monthly evaluations per agent
- new_hire_classes       — one in-progress class
- new_hire_progress      — weekly nesting snapshots for class members

Usage:
    python -m scripts.generate_wave3_4_data \\
        --window-days 28 --seed 42

Idempotency: truncates the Wave 3+4 tables before inserting, so re-runs
replace the synthetic content cleanly. Does NOT touch agents, schedules,
or interval_history.
"""
from __future__ import annotations

import argparse
import math
import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


# -----------------------------------------------------------------------------
# Aux-code vocabulary (kept in sync with 0014 comment).
# -----------------------------------------------------------------------------

WORK_AUX = "available"  # we collapse on_call/acw into available — adherence is per planned-state, not per call-step
PLAN_TO_AUX = {
    "work": "available",
    "break": "break",
    "lunch": "lunch",
    "training": "training",
    "off": "offline",
}


@dataclass
class Shift:
    agent_id: int
    schedule_id: int
    segments: list[dict]  # [{start, end, segment_type}]


# -----------------------------------------------------------------------------
# Core generators.
# -----------------------------------------------------------------------------


def _sim_now(db: Session) -> datetime:
    return db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]


def truncate_wave_tables(db: Session) -> None:
    """Truncate the Wave 3+4 tables in dependency order."""
    tables = [
        "adherence_exceptions",
        "agent_aux_events",
        "pto_ledger",
        "leave_requests",
        "training_attendees",
        "training_events",
        "agent_certifications",
        "agent_qa_scores",
        "new_hire_progress",
        "new_hire_classes",
    ]
    for t in tables:
        db.execute(text(f"TRUNCATE TABLE {t} RESTART IDENTITY CASCADE"))
    db.commit()


def fetch_shifts_window(
    db: Session, start_ts: datetime, end_ts: datetime
) -> list[dict]:
    """Pull shift_segments grouped by (agent_id, calendar day) inside the window."""
    rows = (
        db.execute(
            text(
                """
                SELECT agent_id, schedule_id, segment_type, start_time, end_time
                FROM shift_segments
                WHERE start_time >= :start AND start_time < :end
                ORDER BY agent_id, start_time
                """
            ),
            {"start": start_ts, "end": end_ts},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def generate_aux_events_and_exceptions(
    db: Session,
    shift_rows: list[dict],
    *,
    rng: random.Random,
    deviation_rate: float = 0.15,
) -> tuple[int, int]:
    """
    For each agent-day with shifts, emit aux events.

    Baseline: 1 aux event per shift_segment, matching plan.
    Deviations (sampled per agent-day at `deviation_rate`):
      - late_start, early_out, missed_break, extended_break, unplanned_aux
    Each deviation writes both an aux event and an adherence_exceptions row.
    """
    # Group rows by (agent_id, day).
    by_agent_day: dict[tuple[int, datetime], list[dict]] = {}
    for r in shift_rows:
        day = r["start_time"].astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        by_agent_day.setdefault((r["agent_id"], day), []).append(r)

    aux_batch: list[dict] = []
    exc_batch: list[dict] = []

    for (agent_id, _day), segs in by_agent_day.items():
        segs = sorted(segs, key=lambda s: s["start_time"])
        # Decide whether to perturb this day.
        deviation: str | None = None
        if rng.random() < deviation_rate:
            deviation = rng.choice(
                [
                    "late_start",
                    "early_out",
                    "missed_break",
                    "extended_break",
                    "unplanned_aux",
                ]
            )

        # Emit aux events with optional perturbation applied.
        events, exceptions = _events_for_day(
            agent_id=agent_id, segments=segs, deviation=deviation, rng=rng
        )
        aux_batch.extend(events)
        exc_batch.extend(exceptions)

    if aux_batch:
        db.execute(
            text(
                """
                INSERT INTO agent_aux_events
                    (agent_id, start_ts, end_ts, aux_code, reason_code)
                VALUES (:agent_id, :start_ts, :end_ts, :aux_code, :reason_code)
                """
            ),
            aux_batch,
        )
    if exc_batch:
        db.execute(
            text(
                """
                INSERT INTO adherence_exceptions
                    (agent_id, start_ts, end_ts, duration_seconds,
                     exception_type, planned_state, actual_state, note)
                VALUES
                    (:agent_id, :start_ts, :end_ts, :duration_seconds,
                     :exception_type, :planned_state, :actual_state, :note)
                """
            ),
            exc_batch,
        )
    db.commit()
    return len(aux_batch), len(exc_batch)


def _events_for_day(
    *,
    agent_id: int,
    segments: list[dict],
    deviation: str | None,
    rng: random.Random,
) -> tuple[list[dict], list[dict]]:
    """Build per-segment aux events for one agent-day; apply deviation if any."""
    aux_events: list[dict] = []
    exceptions: list[dict] = []

    # Start from baseline: every segment → exactly one aux event matching plan.
    baseline = [
        {
            "agent_id": agent_id,
            "start_ts": s["start_time"],
            "end_ts": s["end_time"],
            "aux_code": PLAN_TO_AUX.get(s["segment_type"], "offline"),
            "reason_code": None,
            "_planned": s["segment_type"],
        }
        for s in segments
    ]

    if not baseline:
        return [], []

    if deviation == "late_start" and baseline[0]["_planned"] == "work":
        # Shrink first event from the left; emit offline event for the gap.
        lateness = timedelta(minutes=rng.randint(5, 30))
        gap_start = baseline[0]["start_ts"]
        new_start = gap_start + lateness
        aux_events.append(
            {
                "agent_id": agent_id,
                "start_ts": gap_start,
                "end_ts": new_start,
                "aux_code": "offline",
                "reason_code": "late",
            }
        )
        baseline[0]["start_ts"] = new_start
        exceptions.append(
            _make_exc(
                agent_id,
                gap_start,
                new_start,
                "late_start",
                "work",
                "offline",
                "Agent logged in late",
            )
        )

    elif deviation == "early_out" and baseline[-1]["_planned"] == "work":
        early = timedelta(minutes=rng.randint(10, 45))
        end = baseline[-1]["end_ts"]
        new_end = end - early
        if new_end > baseline[-1]["start_ts"]:
            baseline[-1]["end_ts"] = new_end
            aux_events.append(
                {
                    "agent_id": agent_id,
                    "start_ts": new_end,
                    "end_ts": end,
                    "aux_code": "offline",
                    "reason_code": "early_out",
                }
            )
            exceptions.append(
                _make_exc(
                    agent_id,
                    new_end,
                    end,
                    "early_out",
                    "work",
                    "offline",
                    "Agent logged out before shift end",
                )
            )

    elif deviation == "missed_break":
        # Find a break segment and drop it from baseline; emit a work-coded
        # event in its place so adherence sees them "working through break".
        for i, b in enumerate(baseline):
            if b["_planned"] == "break":
                exceptions.append(
                    _make_exc(
                        agent_id,
                        b["start_ts"],
                        b["end_ts"],
                        "missed_break",
                        "break",
                        "available",
                        "Agent stayed available through scheduled break",
                    )
                )
                b["aux_code"] = "available"
                b["reason_code"] = "missed_break"
                break

    elif deviation == "extended_break":
        for i, b in enumerate(baseline):
            if b["_planned"] == "break" and i + 1 < len(baseline):
                extra = timedelta(minutes=rng.randint(5, 15))
                old_end = b["end_ts"]
                new_end = old_end + extra
                # Cap to next segment start to avoid overlap.
                next_start = baseline[i + 1]["start_ts"]
                if new_end > next_start:
                    new_end = next_start
                if new_end > old_end:
                    b["end_ts"] = new_end
                    # Shrink the following work segment.
                    baseline[i + 1]["start_ts"] = new_end
                    exceptions.append(
                        _make_exc(
                            agent_id,
                            old_end,
                            new_end,
                            "extended_break",
                            "work",
                            "break",
                            "Break ran past scheduled return",
                        )
                    )
                break

    elif deviation == "unplanned_aux":
        # Splice an unplanned aux into a work segment.
        for i, b in enumerate(baseline):
            if b["_planned"] == "work":
                seg_dur = (b["end_ts"] - b["start_ts"]).total_seconds()
                if seg_dur < 1800:  # need at least 30 min
                    continue
                dur = timedelta(minutes=rng.randint(5, 20))
                offset = timedelta(
                    seconds=rng.uniform(600, seg_dur - dur.total_seconds() - 600)
                )
                aux_start = b["start_ts"] + offset
                aux_end = aux_start + dur
                aux_code = rng.choice(["system", "coaching", "meeting"])
                # Split the work segment into two.
                left = dict(b)
                left["end_ts"] = aux_start
                right = dict(b)
                right["start_ts"] = aux_end
                baseline[i] = left
                baseline.insert(i + 1, right)
                aux_events.append(
                    {
                        "agent_id": agent_id,
                        "start_ts": aux_start,
                        "end_ts": aux_end,
                        "aux_code": aux_code,
                        "reason_code": "unplanned",
                    }
                )
                exceptions.append(
                    _make_exc(
                        agent_id,
                        aux_start,
                        aux_end,
                        "unplanned_aux",
                        "work",
                        aux_code,
                        f"Unplanned {aux_code} during scheduled work",
                    )
                )
                break

    # Strip the bookkeeping field before insert.
    for b in baseline:
        aux_events.append(
            {
                "agent_id": b["agent_id"],
                "start_ts": b["start_ts"],
                "end_ts": b["end_ts"],
                "aux_code": b["aux_code"],
                "reason_code": b["reason_code"],
            }
        )
    return aux_events, exceptions


def _make_exc(
    agent_id: int,
    start: datetime,
    end: datetime,
    exc_type: str,
    planned: str,
    actual: str,
    note: str,
) -> dict:
    return {
        "agent_id": agent_id,
        "start_ts": start,
        "end_ts": end,
        "duration_seconds": int((end - start).total_seconds()),
        "exception_type": exc_type,
        "planned_state": planned,
        "actual_state": actual,
        "note": note,
    }


# -----------------------------------------------------------------------------
# PTO + leave.
# -----------------------------------------------------------------------------


def generate_pto_and_leave(
    db: Session, *, sim_now: datetime, rng: random.Random
) -> tuple[int, int]:
    agents = (
        db.execute(text("SELECT id, hire_date FROM agents WHERE active = TRUE"))
        .mappings()
        .all()
    )
    ledger_batch: list[dict] = []
    leave_batch: list[dict] = []
    accrual_per_period = 3.08  # ~80h/year over 26 bi-weekly periods

    for a in agents:
        agent_id = a["id"]
        # Build ledger: opening balance 80h 12 months ago + bi-weekly accruals +
        # 0–4 random uses.
        balance = 80.0
        start_dt = sim_now - timedelta(days=365)
        ledger_batch.append(
            {
                "agent_id": agent_id,
                "event_ts": start_dt,
                "event_type": "adjust",
                "hours": 80.0,
                "balance_after": balance,
                "note": "Opening balance",
            }
        )
        dt = start_dt
        while dt < sim_now:
            dt = dt + timedelta(days=14)
            if dt >= sim_now:
                break
            balance += accrual_per_period
            ledger_batch.append(
                {
                    "agent_id": agent_id,
                    "event_ts": dt,
                    "event_type": "accrual",
                    "hours": accrual_per_period,
                    "balance_after": round(balance, 2),
                    "note": "Bi-weekly accrual",
                }
            )

        # Random PTO uses in the past year.
        for _ in range(rng.randint(0, 4)):
            used_hours = float(rng.choice([8, 8, 16, 24]))
            used_ts = start_dt + timedelta(days=rng.randint(14, 350))
            if used_ts >= sim_now:
                continue
            balance -= used_hours
            ledger_batch.append(
                {
                    "agent_id": agent_id,
                    "event_ts": used_ts,
                    "event_type": "use",
                    "hours": -used_hours,
                    "balance_after": round(balance, 2),
                    "note": "PTO used",
                }
            )

    # Leave requests — across the agent pool.
    # Pending: ~20 in the next 30 days
    # Approved: ~30 in past 60 days
    # Denied: ~5 in past 60 days
    agent_ids = [a["id"] for a in agents]
    for _ in range(20):
        agent_id = rng.choice(agent_ids)
        days_ahead = rng.randint(1, 30)
        dur_days = rng.randint(1, 5)
        start = (sim_now + timedelta(days=days_ahead)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=dur_days, hours=8)
        leave_batch.append(
            {
                "agent_id": agent_id,
                "requested_at": sim_now - timedelta(days=rng.randint(0, 7)),
                "start_ts": start,
                "end_ts": end,
                "leave_type": rng.choices(
                    ["PTO", "sick", "unpaid", "swap"], weights=[0.7, 0.15, 0.1, 0.05]
                )[0],
                "status": "pending",
                "reason": rng.choice(
                    [None, "Family event", "Travel", "Appointment", "Personal"]
                ),
                "decided_at": None,
                "decided_by": None,
                "decision_note": None,
            }
        )
    for _ in range(30):
        agent_id = rng.choice(agent_ids)
        days_back = rng.randint(1, 60)
        dur_days = rng.randint(1, 5)
        start = (sim_now - timedelta(days=days_back)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=dur_days, hours=8)
        leave_batch.append(
            {
                "agent_id": agent_id,
                "requested_at": start - timedelta(days=14),
                "start_ts": start,
                "end_ts": end,
                "leave_type": rng.choices(
                    ["PTO", "sick"], weights=[0.85, 0.15]
                )[0],
                "status": "approved",
                "reason": rng.choice([None, "Approved request", "Family event"]),
                "decided_at": start - timedelta(days=7),
                "decided_by": "ops_manager",
                "decision_note": "Approved within SL impact threshold.",
            }
        )
    for _ in range(5):
        agent_id = rng.choice(agent_ids)
        days_back = rng.randint(1, 60)
        start = (sim_now - timedelta(days=days_back)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=1, hours=8)
        leave_batch.append(
            {
                "agent_id": agent_id,
                "requested_at": start - timedelta(days=10),
                "start_ts": start,
                "end_ts": end,
                "leave_type": "PTO",
                "status": "denied",
                "reason": "Blackout date",
                "decided_at": start - timedelta(days=5),
                "decided_by": "ops_manager",
                "decision_note": "SL impact > threshold; alternate dates offered.",
            }
        )

    if ledger_batch:
        db.execute(
            text(
                """
                INSERT INTO pto_ledger
                    (agent_id, event_ts, event_type, hours, balance_after, note)
                VALUES
                    (:agent_id, :event_ts, :event_type, :hours, :balance_after, :note)
                """
            ),
            ledger_batch,
        )
    if leave_batch:
        db.execute(
            text(
                """
                INSERT INTO leave_requests
                    (agent_id, requested_at, start_ts, end_ts, leave_type,
                     status, reason, decided_at, decided_by, decision_note)
                VALUES
                    (:agent_id, :requested_at, :start_ts, :end_ts, :leave_type,
                     :status, :reason, :decided_at, :decided_by, :decision_note)
                """
            ),
            leave_batch,
        )
    db.commit()
    return len(ledger_batch), len(leave_batch)


# -----------------------------------------------------------------------------
# Training, certifications, QA, new-hire class.
# -----------------------------------------------------------------------------


def generate_training_certs_qa(
    db: Session, *, sim_now: datetime, rng: random.Random
) -> dict[str, int]:
    agents = (
        db.execute(
            text("SELECT id, hire_date FROM agents WHERE active = TRUE")
        )
        .mappings()
        .all()
    )
    skills = (
        db.execute(text("SELECT id, name FROM skills ORDER BY name"))
        .mappings()
        .all()
    )
    agent_skills_rows = (
        db.execute(
            text("SELECT agent_id, skill_id, proficiency FROM agent_skills")
        )
        .mappings()
        .all()
    )

    counts = {"training": 0, "certs": 0, "qa": 0, "class": 0, "progress": 0}

    # ----- Training events -----
    training_rows: list[dict] = []
    attendee_rows: list[dict] = []

    # Weekly team meetings — 1 per skill, every Tuesday at 13:00 for 30 min,
    # spanning -28 to +14 days.
    base_dt = (sim_now - timedelta(days=28)).replace(
        hour=13, minute=0, second=0, microsecond=0
    )
    for week_offset in range(6):
        dt = base_dt + timedelta(weeks=week_offset)
        # Snap to Tuesday.
        dt = dt + timedelta(days=(1 - dt.weekday()) % 7)
        for sk in skills:
            training_rows.append(
                {
                    "event_type": "team_meeting",
                    "title": f"{sk['name'].title()} team standup",
                    "start_ts": dt,
                    "end_ts": dt + timedelta(minutes=30),
                    "required": True,
                    "target_skill_id": sk["id"],
                    "notes": "Weekly recurring team huddle.",
                }
            )

    # Coaching slots — 1 per coachable agent in the next 14 days.
    coachable = rng.sample(agents, k=min(12, len(agents)))
    for a in coachable:
        days_ahead = rng.randint(1, 14)
        dt = (sim_now + timedelta(days=days_ahead)).replace(
            hour=rng.choice([10, 14, 15]), minute=0, second=0, microsecond=0
        )
        training_rows.append(
            {
                "event_type": "coaching",
                "title": f"1:1 coaching — agent {a['id']}",
                "start_ts": dt,
                "end_ts": dt + timedelta(minutes=45),
                "required": True,
                "target_skill_id": None,
                "notes": f"Coaching for agent {a['id']}",
            }
        )

    # One skill certification event next month.
    cert_dt = (sim_now + timedelta(days=10)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    for sk in skills[:2]:
        training_rows.append(
            {
                "event_type": "skill_cert",
                "title": f"{sk['name'].title()} re-certification",
                "start_ts": cert_dt,
                "end_ts": cert_dt + timedelta(hours=2),
                "required": False,
                "target_skill_id": sk["id"],
                "notes": "Quarterly skill re-cert.",
            }
        )

    # Insert training events.
    res = db.execute(
        text(
            """
            INSERT INTO training_events
                (event_type, title, start_ts, end_ts, required, target_skill_id, notes)
            VALUES
                (:event_type, :title, :start_ts, :end_ts, :required, :target_skill_id, :notes)
            RETURNING id, event_type, target_skill_id
            """
        ),
        training_rows,
    )
    # Note: executemany with RETURNING isn't portable; do it row-by-row to capture ids.
    db.commit()
    counts["training"] = len(training_rows)

    # Now attendees — pull back the inserted rows and assign.
    inserted = (
        db.execute(
            text(
                "SELECT id, event_type, target_skill_id FROM training_events "
                "ORDER BY id DESC LIMIT :n"
            ),
            {"n": len(training_rows)},
        )
        .mappings()
        .all()
    )
    skill_to_agents: dict[int, list[int]] = {}
    for r in agent_skills_rows:
        skill_to_agents.setdefault(r["skill_id"], []).append(r["agent_id"])

    for ev in inserted:
        if ev["event_type"] == "team_meeting" and ev["target_skill_id"] in skill_to_agents:
            for agent_id in skill_to_agents[ev["target_skill_id"]]:
                attendee_rows.append(
                    {
                        "training_event_id": ev["id"],
                        "agent_id": agent_id,
                        "attended": None,
                    }
                )
        elif ev["event_type"] == "coaching":
            # Pull the agent_id out of the title.
            agent_id = int(ev_title_to_agent(ev["id"], db))
            if agent_id:
                attendee_rows.append(
                    {
                        "training_event_id": ev["id"],
                        "agent_id": agent_id,
                        "attended": None,
                    }
                )
        elif ev["event_type"] == "skill_cert" and ev["target_skill_id"] in skill_to_agents:
            for agent_id in skill_to_agents[ev["target_skill_id"]]:
                attendee_rows.append(
                    {
                        "training_event_id": ev["id"],
                        "agent_id": agent_id,
                        "attended": None,
                    }
                )

    if attendee_rows:
        db.execute(
            text(
                """
                INSERT INTO training_attendees
                    (training_event_id, agent_id, attended)
                VALUES (:training_event_id, :agent_id, :attended)
                ON CONFLICT DO NOTHING
                """
            ),
            attendee_rows,
        )
        db.commit()

    # ----- Certifications -----
    cert_batch: list[dict] = []
    for r in agent_skills_rows:
        # Certify at the agent's current proficiency, ~6 months ago.
        cert_batch.append(
            {
                "agent_id": r["agent_id"],
                "skill_id": r["skill_id"],
                "level": r["proficiency"],
                "certified_at": sim_now - timedelta(days=rng.randint(60, 365)),
                "expires_at": sim_now + timedelta(days=rng.randint(120, 540)),
                "certifier": "training_team",
            }
        )
    if cert_batch:
        db.execute(
            text(
                """
                INSERT INTO agent_certifications
                    (agent_id, skill_id, level, certified_at, expires_at, certifier)
                VALUES
                    (:agent_id, :skill_id, :level, :certified_at, :expires_at, :certifier)
                ON CONFLICT DO NOTHING
                """
            ),
            cert_batch,
        )
        db.commit()
    counts["certs"] = len(cert_batch)

    # ----- QA scores -----
    qa_batch: list[dict] = []
    # Each agent has a "true" mean score 75–95, with sigma 4.
    for a in agents:
        mu = rng.uniform(75, 95)
        for month_back in range(6):
            dt = sim_now - timedelta(days=30 * month_back + rng.randint(0, 28))
            score = max(40.0, min(100.0, rng.gauss(mu, 4.0)))
            qa_batch.append(
                {
                    "agent_id": a["id"],
                    "evaluated_at": dt,
                    "score": round(score, 1),
                    "sample_size": rng.choice([3, 4, 5]),
                    "reviewer": rng.choice(["qa_alex", "qa_bri", "qa_chen"]),
                    "note": None,
                }
            )
    if qa_batch:
        db.execute(
            text(
                """
                INSERT INTO agent_qa_scores
                    (agent_id, evaluated_at, score, sample_size, reviewer, note)
                VALUES
                    (:agent_id, :evaluated_at, :score, :sample_size, :reviewer, :note)
                """
            ),
            qa_batch,
        )
        db.commit()
    counts["qa"] = len(qa_batch)

    # ----- New-hire class -----
    if skills:
        target_skill = skills[0]
        class_start = sim_now - timedelta(days=21)
        class_end = sim_now + timedelta(days=21)  # 6-week program
        class_id_row = (
            db.execute(
                text(
                    """
                    INSERT INTO new_hire_classes
                        (class_name, start_date, end_date, target_skill_id,
                         target_size, status, notes)
                    VALUES
                        (:name, :start, :end, :skill, :size, :status, :notes)
                    RETURNING id
                    """
                ),
                {
                    "name": f"{target_skill['name'].title()} Class 2026-Q2",
                    "start": class_start.date(),
                    "end": class_end.date(),
                    "skill": target_skill["id"],
                    "size": 6,
                    "status": "in_class",
                    "notes": "First-half classroom, second-half nesting.",
                },
            )
            .mappings()
            .one()
        )
        class_id = class_id_row["id"]
        db.commit()
        counts["class"] = 1

        # 6 new hires: pick the most-recently-hired 6 agents.
        new_hires = (
            db.execute(
                text(
                    "SELECT id FROM agents ORDER BY hire_date DESC NULLS LAST LIMIT 6"
                )
            )
            .mappings()
            .all()
        )
        progress_batch: list[dict] = []
        for nh in new_hires:
            for week in range(1, 4):  # weeks 1-3 of nesting
                eval_dt = class_start + timedelta(weeks=week)
                if eval_dt > sim_now:
                    continue
                progress_batch.append(
                    {
                        "class_id": class_id,
                        "agent_id": nh["id"],
                        "evaluated_at": eval_dt,
                        "nesting_week": week,
                        "qa_score": round(rng.uniform(60, 92), 1),
                        "aht_seconds": round(rng.uniform(420, 720), 1),
                        "adherence_pct": round(rng.uniform(0.85, 0.99), 4),
                        "status": rng.choices(
                            ["on_track", "watch", "at_risk", "washed_out"],
                            weights=[0.6, 0.25, 0.1, 0.05],
                        )[0],
                    }
                )
        if progress_batch:
            db.execute(
                text(
                    """
                    INSERT INTO new_hire_progress
                        (class_id, agent_id, evaluated_at, nesting_week,
                         qa_score, aht_seconds, adherence_pct, status)
                    VALUES
                        (:class_id, :agent_id, :evaluated_at, :nesting_week,
                         :qa_score, :aht_seconds, :adherence_pct, :status)
                    ON CONFLICT DO NOTHING
                    """
                ),
                progress_batch,
            )
            db.commit()
        counts["progress"] = len(progress_batch)

    return counts


def ev_title_to_agent(event_id: int, db: Session) -> str:
    """Extract agent_id from coaching event title 'agent N'."""
    row = (
        db.execute(
            text("SELECT title FROM training_events WHERE id = :id"),
            {"id": event_id},
        )
        .mappings()
        .one()
    )
    title = row["title"]
    parts = title.rsplit(" ", 1)
    if parts and parts[-1].isdigit():
        return parts[-1]
    return ""


# -----------------------------------------------------------------------------
# Entrypoint.
# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--window-days",
        type=int,
        default=28,
        help="Generate aux events from (sim_now - window/2) to (sim_now + window/2).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--deviation-rate",
        type=float,
        default=0.15,
        help="Probability that an agent-day has an adherence deviation.",
    )
    args = parser.parse_args()

    engine: Engine = create_engine(get_settings().database_url, future=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db: Session = SessionLocal()

    rng = random.Random(args.seed)
    sim_now = _sim_now(db)
    print(f"sim_now = {sim_now.isoformat()}")

    half = timedelta(days=args.window_days / 2)
    win_start = sim_now - half
    win_end = sim_now + half
    print(f"Aux event window: {win_start.date()} → {win_end.date()}")

    print("Truncating Wave 3+4 tables…")
    truncate_wave_tables(db)

    print("Fetching shift segments…")
    shifts = fetch_shifts_window(db, win_start, win_end)
    print(f"  {len(shifts)} shift_segments in window.")

    print("Generating aux events + exceptions…")
    aux_n, exc_n = generate_aux_events_and_exceptions(
        db, shifts, rng=rng, deviation_rate=args.deviation_rate
    )
    print(f"  aux_events: {aux_n}, exceptions: {exc_n}")

    print("Generating PTO ledger + leave requests…")
    pto_n, leave_n = generate_pto_and_leave(db, sim_now=sim_now, rng=rng)
    print(f"  pto_ledger: {pto_n}, leave_requests: {leave_n}")

    print("Generating training, certs, QA, new-hire class…")
    counts = generate_training_certs_qa(db, sim_now=sim_now, rng=rng)
    print(f"  {counts}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
