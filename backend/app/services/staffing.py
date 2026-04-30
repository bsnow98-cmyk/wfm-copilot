"""
Erlang C staffing math + StaffingService.

We implement Erlang B/C ourselves (rather than depending on pyworkforce) for
two reasons:
    1. The math is small and well-understood — ~30 lines of code that
       documents itself.
    2. It avoids a dep that gets loaded for one purpose, removing a
       maintenance vector.

The recursive Erlang B form is numerically stable up to a few thousand servers,
which covers any realistic contact-center scenario.

REFERENCES
----------
- Robbins-Monro / Cooper, "Introduction to Queueing Theory", chapter on M/M/c.
- Cleveland, "Call Center Management on Fast Forward" (the operational bible).

QUICK SANITY CHECK
------------------
Standard textbook example: offered load A = 10 Erlangs (300 calls/hour @ 120s
AHT in a 1-hour interval), target SL 80% answered within 20s. Required agents:
N = 14 raw. Tested in the verification step.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import TypedDict

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("wfm.staffing")


# --------------------------------------------------------------------------
# Pure math — no DB, no I/O. Easy to unit test in isolation.
# --------------------------------------------------------------------------
def erlang_b(n: int, a: float) -> float:
    """Erlang B: probability that all N servers are busy with offered load A.

    Recursive form: B(n, A) = (A * B(n-1, A)) / (n + A * B(n-1, A))
    with B(0, A) = 1. This avoids the n!  / A^n explosion of the closed form
    and stays numerically stable through n ~= a few thousand.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if a <= 0:
        return 0.0
    b = 1.0
    for k in range(1, n + 1):
        b = (a * b) / (k + a * b)
    return b


def erlang_c(n: int, a: float) -> float:
    """Erlang C: probability of having to wait > 0.

    C(n, A) = (n * B(n, A)) / (n - A * (1 - B(n, A)))
    Defined only when n > A. If n <= A the system is unstable; we return 1.0
    (everyone waits forever).
    """
    if n <= a:
        return 1.0
    b = erlang_b(n, a)
    return (n * b) / (n - a * (1 - b))


def service_level_pct(
    n: int, lam_per_sec: float, aht_sec: float, target_answer_sec: float
) -> float:
    """P(wait <= target_answer_sec) under Erlang C.

    Formula: SL = 1 - C(n, A) * exp(-(n - A) * T / AHT)
    where A = lam * AHT (offered load in Erlangs), T = target answer time.
    """
    a = lam_per_sec * aht_sec
    if n <= a:
        return 0.0
    c = erlang_c(n, a)
    return 1.0 - c * math.exp(-(n - a) * target_answer_sec / aht_sec)


def expected_asa_sec(n: int, lam_per_sec: float, aht_sec: float) -> float:
    """Expected average speed of answer (waiting time) under Erlang C."""
    a = lam_per_sec * aht_sec
    if n <= a:
        return float("inf")
    c = erlang_c(n, a)
    return (c * aht_sec) / (n - a)


# --------------------------------------------------------------------------
# Required-agents search — find the smallest N that meets the SL target.
# --------------------------------------------------------------------------
class StaffingResult(TypedDict):
    required_agents_raw: int
    required_agents: int
    expected_service_level: float
    expected_asa_seconds: float
    occupancy: float


def required_agents(
    forecast_offered: float,
    aht_seconds: float,
    interval_seconds: int = 1800,   # 30 min
    sl_target: float | None = 0.80,
    target_answer_sec: int = 20,
    target_asa_sec: float | None = 30.0,   # NEW: ASA ceiling
    shrinkage: float = 0.30,
    max_agents: int = 2000,
) -> StaffingResult:
    """Smallest N satisfying ALL active staffing constraints.

    Constraints (set any to None to disable that constraint):
      - service level: P(wait <= target_answer_sec) >= sl_target
      - ASA:           E[wait] <= target_asa_sec

    Most contact centers manage to BOTH simultaneously — e.g. "80% in 20s
    AND average wait <= 30s". Defaults reflect that common policy.

    Parameters
    ----------
    forecast_offered     contacts expected in the interval
    aht_seconds          average handle time
    interval_seconds     length of the interval (1800 = 30 min)
    sl_target            service level target, e.g. 0.80. None = skip SL constraint.
    target_answer_sec    target answer time for SL, e.g. 20.
    target_asa_sec       max acceptable average wait time, e.g. 30. None = skip
                         ASA constraint. This is what most ops actually manage to.
    shrinkage            fraction of paid time NOT productive (breaks, training,
                         meetings, off-phone). Required staffing is grossed up
                         by 1/(1-shrinkage). 0.30 is a typical starting point.

    Returns
    -------
    StaffingResult dict with raw and shrinkage-adjusted required_agents,
    plus expected SL/ASA/occupancy at the raw N.
    """
    # Trivial case: no demand -> no staff. Avoid division-by-zero downstream.
    if forecast_offered <= 0 or aht_seconds <= 0:
        return StaffingResult(
            required_agents_raw=0,
            required_agents=0,
            expected_service_level=1.0,
            expected_asa_seconds=0.0,
            occupancy=0.0,
        )

    if sl_target is None and target_asa_sec is None:
        raise ValueError(
            "At least one of sl_target or target_asa_sec must be set "
            "(otherwise there's no objective to staff to)."
        )

    lam_per_sec = forecast_offered / interval_seconds
    a = lam_per_sec * aht_seconds  # offered load in Erlangs

    # Lower bound: need n > A for stability. Start at ceil(A) (or 1 minimum).
    n = max(1, math.ceil(a))

    # Walk up until ALL active constraints are satisfied (or we hit the safety cap).
    while n < max_agents:
        sl_ok = True
        if sl_target is not None:
            sl = service_level_pct(n, lam_per_sec, aht_seconds, target_answer_sec)
            sl_ok = sl >= sl_target

        asa_ok = True
        if target_asa_sec is not None:
            asa = expected_asa_sec(n, lam_per_sec, aht_seconds)
            asa_ok = asa <= target_asa_sec

        if sl_ok and asa_ok:
            break
        n += 1
    else:
        log.warning(
            "required_agents: hit max_agents=%d for offered=%s aht=%s — "
            "system likely unstable or target unreachable.",
            max_agents, forecast_offered, aht_seconds,
        )

    sl_at_n = service_level_pct(n, lam_per_sec, aht_seconds, target_answer_sec)
    asa = expected_asa_sec(n, lam_per_sec, aht_seconds)
    occ = a / n if n > 0 else 0.0

    raw = n
    if 0 <= shrinkage < 1:
        with_shrink = math.ceil(n / (1 - shrinkage))
    else:
        with_shrink = n  # bad shrinkage value; fail-safe

    return StaffingResult(
        required_agents_raw=raw,
        required_agents=with_shrink,
        expected_service_level=sl_at_n,
        expected_asa_seconds=asa if math.isfinite(asa) else 0.0,
        occupancy=min(occ, 1.0),
    )


# --------------------------------------------------------------------------
# Service — orchestrates DB I/O around the math.
# --------------------------------------------------------------------------
class StaffingService:
    def __init__(self, db: Session):
        self.db = db

    def compute(
        self,
        forecast_run_id: int,
        service_level_target: float | None,
        target_answer_seconds: int,
        shrinkage: float,
        target_asa_seconds: int | None = 30,
    ) -> int:
        """Run Erlang C across all forecast intervals, persist the results.

        Returns the new staffing_requirements.id.
        """
        # Verify forecast exists and is completed.
        run = self.db.execute(
            text("""
                SELECT id, status FROM forecast_runs WHERE id = :id
            """),
            {"id": forecast_run_id},
        ).mappings().first()
        if not run:
            raise ValueError(f"forecast_run_id {forecast_run_id} not found")
        if run["status"] != "completed":
            raise ValueError(
                f"forecast_run_id {forecast_run_id} is in status={run['status']!r}; "
                f"only completed runs can be staffed."
            )

        # Pull forecast intervals.
        intervals = self.db.execute(
            text("""
                SELECT interval_start, forecast_offered, forecast_aht_seconds
                FROM forecast_intervals
                WHERE forecast_run_id = :id
                ORDER BY interval_start
            """),
            {"id": forecast_run_id},
        ).mappings().all()
        if not intervals:
            raise ValueError(f"No forecast_intervals for run {forecast_run_id}")

        # Insert (or replace) the parent row. UPSERT on the unique constraint
        # so re-running with the same params is idempotent. target_asa_seconds
        # is NOT in the unique — re-running with same SL/AT/Shrink but different
        # ASA updates the row in place (latest wins).
        parent = self.db.execute(
            text("""
                INSERT INTO staffing_requirements
                    (forecast_run_id, service_level_target, target_answer_seconds,
                     shrinkage, target_asa_seconds)
                VALUES (:fid, :sl, :tas, :shr, :asa)
                ON CONFLICT (forecast_run_id, service_level_target, target_answer_seconds, shrinkage)
                DO UPDATE SET created_at = NOW(),
                              target_asa_seconds = EXCLUDED.target_asa_seconds
                RETURNING id
            """),
            {
                "fid": forecast_run_id,
                "sl": service_level_target,
                "tas": target_answer_seconds,
                "shr": shrinkage,
                "asa": target_asa_seconds,
            },
        ).fetchone()
        staffing_id = int(parent[0])

        # Wipe any stale interval rows from a previous run with the same params.
        self.db.execute(
            text("DELETE FROM staffing_requirement_intervals WHERE staffing_id = :id"),
            {"id": staffing_id},
        )

        # Compute per-interval requirements. This is fast (sub-second for ~700
        # intervals on a laptop), so we do it inline rather than in a background
        # task.
        rows = []
        for iv in intervals:
            offered = float(iv["forecast_offered"])
            aht = float(iv["forecast_aht_seconds"]) if iv["forecast_aht_seconds"] else 0.0
            res = required_agents(
                forecast_offered=offered,
                aht_seconds=aht,
                interval_seconds=1800,
                sl_target=float(service_level_target) if service_level_target is not None else None,
                target_answer_sec=int(target_answer_seconds),
                target_asa_sec=float(target_asa_seconds) if target_asa_seconds is not None else None,
                shrinkage=float(shrinkage),
            )
            rows.append({
                "sid": staffing_id,
                "ds": iv["interval_start"],
                "offered": offered,
                "aht": aht,
                "raw": res["required_agents_raw"],
                "req": res["required_agents"],
                "sl": res["expected_service_level"],
                "asa": res["expected_asa_seconds"],
                "occ": res["occupancy"],
            })

        self.db.execute(
            text("""
                INSERT INTO staffing_requirement_intervals
                    (staffing_id, interval_start,
                     forecast_offered, forecast_aht_seconds,
                     required_agents_raw, required_agents,
                     expected_service_level, expected_asa_seconds, occupancy)
                VALUES
                    (:sid, :ds, :offered, :aht, :raw, :req, :sl, :asa, :occ)
            """),
            rows,
        )
        self.db.commit()
        log.info(
            "Computed staffing %s for forecast %s — %d intervals, peak required = %d",
            staffing_id, forecast_run_id, len(rows),
            max((r["req"] for r in rows), default=0),
        )
        return staffing_id
