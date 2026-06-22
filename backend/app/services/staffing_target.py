"""
staffing_target — change an SL/ASA target and recompute staffing (Surface #5).

Unlike the sync surfaces, applying a target change kicks off an async recompute
job (Erlang C over the forecast horizon) — same BackgroundTasks + status-column
pattern as the schedule solver. The apply path writes the audit row in
'pending'; the background job recomputes the current scenario IN PLACE (updates
the staffing_requirements targets + its intervals together, so the scenario and
its rows are never inconsistent) and flips the row to 'completed'.

v1 changes the whole-horizon targets of the single current scenario; per-window
targets are a v2 schema. applied_by literal 'demo' until RBAC.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.staffing import required_agents

log = logging.getLogger("wfm.staffing_target")

UNDO_WINDOW = timedelta(hours=24)
# Keys that make up a target set. target_asa_seconds / sl may be null.
TARGET_KEYS = ("sl", "target_answer_seconds", "target_asa_seconds", "shrinkage")


class StaleVersionError(Exception):
    def __init__(
        self, your_version: int, current_version: int, staffing_id: int | None = None
    ) -> None:
        super().__init__(
            f"staffing targets changed (yours={your_version}, current={current_version})"
        )
        self.your_version = your_version
        self.current_version = current_version
        self.staffing_id = staffing_id


class StaffingNotFound(Exception):
    pass


class ChangeNotFound(Exception):
    pass


class AlreadyUndone(Exception):
    pass


class UndoWindowExpired(Exception):
    pass


@dataclass(frozen=True)
class ApplyResult:
    log_id: str
    staffing_id: int
    before_targets: dict[str, Any]
    after_targets: dict[str, Any]
    peak_required_before: int
    applied_at: datetime


@dataclass(frozen=True)
class UndoResult:
    log_id: str
    staffing_id: int
    restored_targets: dict[str, Any]
    peak_required_after: int
    undone_at: datetime
    summary: str


def compute_targets_version(targets: dict[str, Any]) -> int:
    """Stable 31-bit hash of a target set — optimistic-concurrency fingerprint."""
    payload = "|".join(f"{k}={targets.get(k)}" for k in TARGET_KEYS)
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def load_targets(
    db: Session, staffing_id: int, *, lock: bool = False
) -> tuple[dict[str, Any], int] | None:
    """Return (targets, forecast_run_id) for a staffing scenario, or None."""
    row = (
        db.execute(
            text(
                f"""
                SELECT forecast_run_id, service_level_target, target_answer_seconds,
                       target_asa_seconds, shrinkage
                FROM staffing_requirements WHERE id = :id
                {"FOR UPDATE" if lock else ""}
                """
            ),
            {"id": staffing_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    targets = {
        "sl": float(row["service_level_target"]) if row["service_level_target"] is not None else None,
        "target_answer_seconds": int(row["target_answer_seconds"]),
        "target_asa_seconds": int(row["target_asa_seconds"]) if row["target_asa_seconds"] is not None else None,
        "shrinkage": float(row["shrinkage"]),
    }
    return targets, int(row["forecast_run_id"])


def peak_required(db: Session, staffing_id: int) -> int:
    """Current peak required_agents across the scenario's intervals."""
    return int(
        db.execute(
            text("SELECT COALESCE(MAX(required_agents), 0) FROM staffing_requirement_intervals WHERE staffing_id = :id"),
            {"id": staffing_id},
        ).scalar()
        or 0
    )


def compute_peak_for_targets(db: Session, forecast_run_id: int, targets: dict[str, Any]) -> int:
    """Peak required_agents if `targets` were applied — read-only (preview)."""
    intervals = db.execute(
        text(
            """
            SELECT forecast_offered, forecast_aht_seconds
            FROM forecast_intervals WHERE forecast_run_id = :id
            """
        ),
        {"id": forecast_run_id},
    ).all()
    peak = 0
    for offered, aht in intervals:
        res = required_agents(
            forecast_offered=float(offered),
            aht_seconds=float(aht) if aht is not None else 0.0,
            interval_seconds=1800,
            sl_target=targets["sl"],
            target_answer_sec=int(targets["target_answer_seconds"]),
            target_asa_sec=float(targets["target_asa_seconds"]) if targets["target_asa_seconds"] is not None else None,
            shrinkage=float(targets["shrinkage"]),
        )
        peak = max(peak, res["required_agents"])
    return peak


def recompute_intervals(db: Session, staffing_id: int, targets: dict[str, Any]) -> int:
    """Update the scenario's targets AND recompute its intervals together so the
    two never disagree. Returns the new peak required. Caller commits."""
    loaded = load_targets(db, staffing_id)
    if loaded is None:
        raise StaffingNotFound(f"staffing scenario {staffing_id} not found")
    _, forecast_run_id = loaded

    db.execute(
        text(
            """
            UPDATE staffing_requirements
            SET service_level_target = :sl, target_answer_seconds = :tas,
                target_asa_seconds = :asa, shrinkage = :shr
            WHERE id = :id
            """
        ),
        {
            "sl": targets["sl"],
            "tas": targets["target_answer_seconds"],
            "asa": targets["target_asa_seconds"],
            "shr": targets["shrinkage"],
            "id": staffing_id,
        },
    )

    intervals = db.execute(
        text(
            """
            SELECT interval_start, forecast_offered, forecast_aht_seconds
            FROM forecast_intervals WHERE forecast_run_id = :id ORDER BY interval_start
            """
        ),
        {"id": forecast_run_id},
    ).mappings().all()

    db.execute(
        text("DELETE FROM staffing_requirement_intervals WHERE staffing_id = :id"),
        {"id": staffing_id},
    )

    rows = []
    peak = 0
    for iv in intervals:
        offered = float(iv["forecast_offered"])
        aht = float(iv["forecast_aht_seconds"]) if iv["forecast_aht_seconds"] else 0.0
        res = required_agents(
            forecast_offered=offered,
            aht_seconds=aht,
            interval_seconds=1800,
            sl_target=targets["sl"],
            target_answer_sec=int(targets["target_answer_seconds"]),
            target_asa_sec=float(targets["target_asa_seconds"]) if targets["target_asa_seconds"] is not None else None,
            shrinkage=float(targets["shrinkage"]),
        )
        peak = max(peak, res["required_agents"])
        rows.append({
            "sid": staffing_id, "ds": iv["interval_start"], "offered": offered, "aht": aht,
            "raw": res["required_agents_raw"], "req": res["required_agents"],
            "sl": res["expected_service_level"], "asa": res["expected_asa_seconds"],
            "occ": res["occupancy"],
        })
    if rows:
        db.execute(
            text(
                """
                INSERT INTO staffing_requirement_intervals
                    (staffing_id, interval_start, forecast_offered, forecast_aht_seconds,
                     required_agents_raw, required_agents, expected_service_level,
                     expected_asa_seconds, occupancy)
                VALUES (:sid, :ds, :offered, :aht, :raw, :req, :sl, :asa, :occ)
                """
            ),
            rows,
        )
    return peak


def summarize_targets(before: dict[str, Any], after: dict[str, Any]) -> str:
    def fmt(t):
        sl = f"{t['sl']*100:.0f}%/{t['target_answer_seconds']}s" if t.get("sl") is not None else "no-SL"
        asa = f", ASA≤{t['target_asa_seconds']}s" if t.get("target_asa_seconds") is not None else ""
        return f"{sl}{asa}, shrink {t['shrinkage']*100:.0f}%"
    return f"Staffing target: {fmt(before)} → {fmt(after)}"


def apply_target_change(
    db: Session,
    *,
    staffing_id: int,
    new_targets: dict[str, Any],
    expected_version: int,
    conversation_id: str | None,
    actor: str = "demo",
) -> ApplyResult:
    """Write the audit row in 'pending' (the recompute itself runs in a
    background job). Raises StaffingNotFound / StaleVersionError. Caller commits
    then schedules the recompute."""
    loaded = load_targets(db, staffing_id, lock=True)
    if loaded is None:
        raise StaffingNotFound(f"staffing scenario {staffing_id} not found")
    before, forecast_run_id = loaded

    current_version = compute_targets_version(before)
    if current_version != expected_version:
        raise StaleVersionError(expected_version, current_version, staffing_id=staffing_id)

    # Merge: only provided keys change.
    after = dict(before)
    for k in TARGET_KEYS:
        if k in new_targets and new_targets[k] is not None:
            after[k] = new_targets[k]

    peak_before = peak_required(db, staffing_id)
    applied_at = datetime.now(timezone.utc)
    log_id = db.execute(
        text(
            """
            INSERT INTO staffing_target_change_log
                (applied_at, applied_by, conversation_id, staffing_id, forecast_run_id,
                 before_targets, after_targets, recompute_status,
                 peak_required_before, undo_window_ends_at)
            VALUES
                (:at, :actor, CAST(:conv AS uuid), :sid, :frid,
                 CAST(:before AS jsonb), CAST(:after AS jsonb), 'pending',
                 :pb, :undo_until)
            RETURNING id
            """
        ),
        {
            "at": applied_at,
            "actor": actor,
            "conv": conversation_id,
            "sid": staffing_id,
            "frid": forecast_run_id,
            "before": json.dumps(before, default=str),
            "after": json.dumps(after, default=str),
            "pb": peak_before,
            "undo_until": applied_at + UNDO_WINDOW,
        },
    ).scalar_one()

    return ApplyResult(
        log_id=str(log_id),
        staffing_id=staffing_id,
        before_targets=before,
        after_targets=after,
        peak_required_before=peak_before,
        applied_at=applied_at,
    )


def run_recompute(session_factory, *, log_id: str, staffing_id: int, after_targets: dict[str, Any]) -> None:
    """Background job: recompute the scenario for after_targets, flip the log to
    completed/failed. Opens its own session (the request's is closed)."""
    from app.services.notifications import notify_staffing_recomputed

    with session_factory() as db:
        try:
            db.execute(
                text(
                    "UPDATE staffing_target_change_log SET recompute_status='running', "
                    "started_at=NOW() WHERE id = CAST(:id AS uuid)"
                ),
                {"id": log_id},
            )
            db.commit()

            peak_after = recompute_intervals(db, staffing_id, after_targets)

            db.execute(
                text(
                    "UPDATE staffing_target_change_log "
                    "SET recompute_status='completed', completed_at=NOW(), "
                    "peak_required_after=:pa WHERE id = CAST(:id AS uuid)"
                ),
                {"pa": peak_after, "id": log_id},
            )
            row = db.execute(
                text("SELECT before_targets, after_targets, peak_required_before FROM staffing_target_change_log WHERE id = CAST(:id AS uuid)"),
                {"id": log_id},
            ).mappings().one()
            summary = (
                summarize_targets(row["before_targets"], row["after_targets"])
                + f" — peak required {row['peak_required_before']} → {peak_after}"
            )
            notify_staffing_recomputed(
                db, summary=summary, log_id=log_id, staffing_id=staffing_id, conversation_id=None
            )
            db.commit()
            log.info("Staffing recompute %s done — peak %s", log_id, peak_after)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            db.execute(
                text(
                    "UPDATE staffing_target_change_log SET recompute_status='failed', "
                    "recompute_error=:e, completed_at=NOW() WHERE id = CAST(:id AS uuid)"
                ),
                {"e": str(exc)[:500], "id": log_id},
            )
            db.commit()
            log.exception("Staffing recompute %s failed", log_id)


def undo_target_change(db: Session, log_id: str) -> UndoResult:
    """Restore the prior targets and recompute inline (fast). Raises
    ChangeNotFound / AlreadyUndone / UndoWindowExpired."""
    row = (
        db.execute(
            text(
                """
                SELECT id, staffing_id, before_targets, undo_window_ends_at, undone_at
                FROM staffing_target_change_log WHERE id = CAST(:id AS uuid) FOR UPDATE
                """
            ),
            {"id": log_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ChangeNotFound(f"staffing_target_change_log {log_id} not found")
    if row["undone_at"] is not None:
        raise AlreadyUndone(f"staffing_target_change_log {log_id} already undone")
    if row["undo_window_ends_at"] < datetime.now(timezone.utc):
        raise UndoWindowExpired(f"staffing_target_change_log {log_id} past the 24h undo window")

    staffing_id = int(row["staffing_id"])
    before = row["before_targets"]
    peak_after = recompute_intervals(db, staffing_id, before)
    undone_at = datetime.now(timezone.utc)
    db.execute(
        text("UPDATE staffing_target_change_log SET undone_at = :at WHERE id = CAST(:id AS uuid)"),
        {"at": undone_at, "id": log_id},
    )
    return UndoResult(
        log_id=str(row["id"]),
        staffing_id=staffing_id,
        restored_targets=before,
        peak_required_after=peak_after,
        undone_at=undone_at,
        summary=f"Reverted staffing targets — peak required restored to {peak_after}",
    )
