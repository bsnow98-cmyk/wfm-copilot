"""
forecast_override — apply + undo an analyst override of one forecast interval.

Surface #4 of EXECUTION_ROADMAP.md. Pins forecast_intervals.forecast_offered for
a (forecast_run_id, interval_start) to an analyst value. Optimistic concurrency:
the version is a hash of the current value — if the forecast was re-run or
re-overridden between preview and apply, the version mismatches → 409. Audit +
24h undo via forecast_override_log. applied_by literal 'demo' until RBAC.

v1 pins the value only. Recomputing downstream staffing is a job (deferred —
pairs with Surface #5 / the async solver); the preview says so.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

UNDO_WINDOW = timedelta(hours=24)


class StaleVersionError(Exception):
    def __init__(
        self,
        your_version: int,
        current_version: int,
        forecast_run_id: int | None = None,
        interval_start: str | None = None,
    ) -> None:
        super().__init__(
            f"forecast value changed (yours={your_version}, current={current_version})"
        )
        self.your_version = your_version
        self.current_version = current_version
        self.forecast_run_id = forecast_run_id
        self.interval_start = interval_start


class IntervalNotFound(Exception):
    pass


class ChangeNotFound(Exception):
    pass


class AlreadyUndone(Exception):
    pass


class UndoWindowExpired(Exception):
    pass


@dataclass(frozen=True)
class OverrideResult:
    log_id: str
    forecast_run_id: int
    interval_start: datetime
    before_value: float
    after_value: float
    applied_at: datetime
    summary: str


@dataclass(frozen=True)
class UndoResult:
    log_id: str
    forecast_run_id: int
    interval_start: datetime
    restored_value: float
    undone_at: datetime
    summary: str


def compute_value_version(value: float) -> int:
    """Stable 31-bit hash of the interval's current value — the optimistic-
    concurrency fingerprint, mirroring the other surfaces' version hashes."""
    payload = f"{float(value):.2f}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def load_interval_value(
    db: Session, forecast_run_id: int, interval_start: str, *, lock: bool = False
) -> float | None:
    row = db.execute(
        text(
            f"""
            SELECT forecast_offered FROM forecast_intervals
            WHERE forecast_run_id = :rid AND interval_start = :ts
            {"FOR UPDATE" if lock else ""}
            """
        ),
        {"rid": forecast_run_id, "ts": interval_start},
    ).scalar_one_or_none()
    return float(row) if row is not None else None


def summarize_override(
    *, interval_start: datetime, before: float, after: float
) -> str:
    return (
        f"Forecast override — {interval_start.strftime('%Y-%m-%d %H:%M')}: "
        f"{before:.0f} → {after:.0f} offered (Δ{after - before:+.0f})"
    )


def apply_override(
    db: Session,
    *,
    forecast_run_id: int,
    interval_start: str,
    new_value: float,
    expected_version: int,
    conversation_id: str | None,
) -> OverrideResult:
    """Pin one forecast interval to new_value inside the caller's txn. Raises
    IntervalNotFound (→404) / StaleVersionError (→409). The router commits."""
    current = load_interval_value(db, forecast_run_id, interval_start, lock=True)
    if current is None:
        raise IntervalNotFound(
            f"interval {interval_start} not found in forecast run {forecast_run_id}"
        )

    current_version = compute_value_version(current)
    if current_version != expected_version:
        raise StaleVersionError(
            expected_version,
            current_version,
            forecast_run_id=forecast_run_id,
            interval_start=interval_start,
        )

    db.execute(
        text(
            """
            UPDATE forecast_intervals SET forecast_offered = :v
            WHERE forecast_run_id = :rid AND interval_start = :ts
            """
        ),
        {"v": new_value, "rid": forecast_run_id, "ts": interval_start},
    )

    applied_at = datetime.now(timezone.utc)
    log_id = db.execute(
        text(
            """
            INSERT INTO forecast_override_log
                (applied_at, applied_by, conversation_id, forecast_run_id,
                 interval_start, before_value, after_value, undo_window_ends_at)
            VALUES
                (:at, 'demo', CAST(:conv AS uuid), :rid, :ts, :before, :after, :undo_until)
            RETURNING id
            """
        ),
        {
            "at": applied_at,
            "conv": conversation_id,
            "rid": forecast_run_id,
            "ts": interval_start,
            "before": current,
            "after": new_value,
            "undo_until": applied_at + UNDO_WINDOW,
        },
    ).scalar_one()

    ts_dt = datetime.fromisoformat(interval_start)
    return OverrideResult(
        log_id=str(log_id),
        forecast_run_id=forecast_run_id,
        interval_start=ts_dt,
        before_value=current,
        after_value=new_value,
        applied_at=applied_at,
        summary=summarize_override(interval_start=ts_dt, before=current, after=new_value),
    )


def undo_override(db: Session, log_id: str) -> UndoResult:
    """Restore the interval's prior value within 24h. Raises ChangeNotFound /
    AlreadyUndone / UndoWindowExpired."""
    row = (
        db.execute(
            text(
                """
                SELECT id, forecast_run_id, interval_start, before_value, after_value,
                       undo_window_ends_at, undone_at
                FROM forecast_override_log
                WHERE id = CAST(:id AS uuid)
                FOR UPDATE
                """
            ),
            {"id": log_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ChangeNotFound(f"forecast_override_log {log_id} not found")
    if row["undone_at"] is not None:
        raise AlreadyUndone(f"forecast_override_log {log_id} already undone")
    if row["undo_window_ends_at"] < datetime.now(timezone.utc):
        raise UndoWindowExpired(f"forecast_override_log {log_id} past the 24h undo window")

    db.execute(
        text(
            """
            UPDATE forecast_intervals SET forecast_offered = :v
            WHERE forecast_run_id = :rid AND interval_start = :ts
            """
        ),
        {"v": row["before_value"], "rid": row["forecast_run_id"], "ts": row["interval_start"]},
    )
    undone_at = datetime.now(timezone.utc)
    db.execute(
        text("UPDATE forecast_override_log SET undone_at = :at WHERE id = CAST(:id AS uuid)"),
        {"at": undone_at, "id": log_id},
    )

    ts_dt = row["interval_start"]
    restored = float(row["before_value"])
    return UndoResult(
        log_id=str(row["id"]),
        forecast_run_id=int(row["forecast_run_id"]),
        interval_start=ts_dt,
        restored_value=restored,
        undone_at=undone_at,
        summary=(
            f"Undid forecast override — {ts_dt.strftime('%Y-%m-%d %H:%M')} "
            f"restored to {restored:.0f} offered"
        ),
    )
