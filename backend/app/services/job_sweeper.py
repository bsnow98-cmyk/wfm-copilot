"""
Orphaned-background-job sweeper.

Schedule solves and forecast runs execute in FastAPI BackgroundTasks, which
die with the process. A deploy or crash mid-job leaves the row stuck at
'pending'/'running' forever — the in-code failure marking only fires on
exceptions, not on process death — and a polling client spins indefinitely.

Run on every API startup. Only rows older than STALE_AFTER_MINUTES are
swept: during a rolling deploy the old instance may still be finishing a
job, and the threshold (comfortably above the 60s solver ceiling and any
plausible forecast fit) keeps us from failing work that is actually alive.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("wfm.job_sweeper")

STALE_AFTER_MINUTES = 15

_INTERRUPTED_MSG = (
    "interrupted: the API process restarted while this job was queued or "
    "running. Re-submit to retry."
)


def sweep_orphaned_jobs(db: Session) -> dict[str, int]:
    """Mark stale pending/running jobs as failed. Returns counts per table."""
    schedules = db.execute(
        text("""
            UPDATE schedules
            SET solver_status = 'failed',
                error_message = :msg,
                completed_at  = NOW()
            WHERE solver_status IN ('pending', 'running')
              AND COALESCE(started_at, created_at)
                  < NOW() - make_interval(mins => :stale)
            RETURNING id
        """),
        {"msg": _INTERRUPTED_MSG, "stale": STALE_AFTER_MINUTES},
    ).fetchall()

    forecasts = db.execute(
        text("""
            UPDATE forecast_runs
            SET status        = 'failed',
                error_message = :msg,
                completed_at  = NOW()
            WHERE status IN ('pending', 'running')
              AND COALESCE(started_at, created_at)
                  < NOW() - make_interval(mins => :stale)
            RETURNING id
        """),
        {"msg": _INTERRUPTED_MSG, "stale": STALE_AFTER_MINUTES},
    ).fetchall()

    db.commit()

    counts = {"schedules": len(schedules), "forecast_runs": len(forecasts)}
    if schedules:
        log.warning(
            "Swept %d orphaned schedule(s): %s",
            len(schedules), [r[0] for r in schedules],
        )
    if forecasts:
        log.warning(
            "Swept %d orphaned forecast run(s): %s",
            len(forecasts), [r[0] for r in forecasts],
        )
    return counts
