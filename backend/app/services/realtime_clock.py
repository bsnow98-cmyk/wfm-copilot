"""
Simulation clock — Wave 3.

The Wave 3+ tools treat "now" as a simulated value, not wall-clock NOW(),
so the live ticker can advance through the synthetic dataset without us
having to keep generating fresh aux events into the future.

Two ways to read sim-now:
- `sim_now(db)` — Python; round-trips to Postgres for the canonical value.
- `sim_now()` SQL function — same answer; use this inside SQL so the
  comparison happens server-side.

`reset_anchor` lets demo/admin code jump the cursor to a chosen sim
timestamp without losing the speed_multiplier (or override that too).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("wfm.realtime_clock")


@dataclass(frozen=True)
class SimAnchor:
    anchor_real_ts: datetime
    anchor_sim_ts: datetime
    speed_multiplier: float
    notes: str | None


def sim_now(db: Session) -> datetime:
    """Return the current simulated timestamp (UTC, tz-aware)."""
    row = db.execute(text("SELECT sim_now() AS ts")).mappings().one()
    return row["ts"]


def sim_today(db: Session) -> date:
    """Return the calendar date of sim-now (UTC). Convenience for tools
    whose 'today' default needs to follow the live ticker."""
    return sim_now(db).date()


def get_anchor(db: Session) -> SimAnchor:
    row = (
        db.execute(
            text(
                "SELECT anchor_real_ts, anchor_sim_ts, speed_multiplier, notes "
                "FROM sim_anchor WHERE id = TRUE"
            )
        )
        .mappings()
        .one()
    )
    return SimAnchor(
        anchor_real_ts=row["anchor_real_ts"],
        anchor_sim_ts=row["anchor_sim_ts"],
        speed_multiplier=float(row["speed_multiplier"]),
        notes=row["notes"],
    )


def ensure_sim_anchor_in_window(db: Session) -> bool:
    """Re-anchor the sim clock if it has drifted outside the seeded data.

    The clock advances with real time, but the synthetic shift_segments
    cover a fixed window — after ~a week of real time, sim-now walks past
    the data and the live ticker / Wave 3+4 tools read into the void.
    Called on every API startup (cheap: two SELECTs, usually a no-op).

    Re-anchors to the mid-window date at sim-now's current time-of-day so
    the intraday position stays natural. Returns True if it re-anchored.
    """
    window = db.execute(
        text("SELECT MIN(start_time) AS lo, MAX(start_time) AS hi FROM shift_segments")
    ).mappings().one()
    lo, hi = window["lo"], window["hi"]
    if lo is None or hi is None:
        return False  # nothing seeded yet — nothing to anchor to

    now = sim_now(db)
    if lo <= now <= hi:
        return False

    mid_date = (lo + (hi - lo) / 2).date()
    target = datetime.combine(mid_date, now.timetz())
    reset_anchor(
        db,
        anchor_sim_ts=target,
        notes=f"Auto re-anchored at startup: sim-now {now.isoformat()} had "
              f"drifted outside the seeded window {lo.date()}..{hi.date()}",
    )
    return True


_last_window_check_monotonic: float = float("-inf")


def maybe_ensure_sim_anchor(db: Session, min_interval_s: float = 600.0) -> bool:
    """Throttled, never-raises wrapper around ensure_sim_anchor_in_window.

    The startup check only covers restarts; on a non-sleeping web tier the
    process can stay up for weeks, long enough for the clock to drift out of
    the data mid-uptime. Call this from hot read paths that depend on
    sim-now being inside the window (e.g. /intraday/today) — it runs the
    real check at most once per min_interval_s and swallows errors so a
    healing hiccup can never fail a read. Returns True if it re-anchored.
    """
    global _last_window_check_monotonic
    now = time.monotonic()
    if now - _last_window_check_monotonic < min_interval_s:
        return False
    _last_window_check_monotonic = now
    try:
        healed = ensure_sim_anchor_in_window(db)
        if healed:
            log.info("Sim clock re-anchored from a read-path check.")
        return healed
    except Exception:
        log.exception("Read-path sim-anchor check failed; serving as-is.")
        return False


def reset_anchor(
    db: Session,
    *,
    anchor_sim_ts: datetime,
    speed_multiplier: float | None = None,
    notes: str | None = None,
) -> SimAnchor:
    """Reset the clock so sim-now == anchor_sim_ts at the moment of the call."""
    params: dict[str, object] = {"sim_ts": anchor_sim_ts}
    set_clauses = [
        "anchor_real_ts = NOW()",
        "anchor_sim_ts = :sim_ts",
        "updated_at = NOW()",
    ]
    if speed_multiplier is not None:
        set_clauses.append("speed_multiplier = :speed")
        params["speed"] = speed_multiplier
    if notes is not None:
        set_clauses.append("notes = :notes")
        params["notes"] = notes
    db.execute(
        text(f"UPDATE sim_anchor SET {', '.join(set_clauses)} WHERE id = TRUE"),
        params,
    )
    db.commit()
    return get_anchor(db)
