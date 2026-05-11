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

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


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
