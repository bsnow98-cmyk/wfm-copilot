"""
Phase 5 — anomaly detection over forecast residuals.

Three detectors, each with a category specialty:

| Detector          | Category              | Signal                                               |
|-------------------|-----------------------|------------------------------------------------------|
| isolation_forest  | volume_spike          | extreme single-interval residuals                    |
| lof               | volume_spike          | density-based outliers in (residual, |residual|) 2D  |
| rolling_mean      | forecast_bias_drift   | rolling window mean of residuals far from zero       |

The detector enum is closed (Decisions.md). Categories are open — adding one
later doesn't require a schema change, only a new branch here.

Adherence breaches piggyback on `rolling_mean` over the schedule_coverage
shortage column when that data is present. We treat them as a separate
category but share the rolling-mean machinery.

Scores are NOT normalized. PyOD-style scores are detector-specific:
- IsolationForest: -score_samples (higher = more anomalous)
- LOF: -negative_outlier_factor_ (higher = more anomalous)
- rolling_mean: |rolling_mean_residual| / interval_std (higher = more drift)

Severity maps from per-detector percentiles within the run, not from absolute
score. This is the honest move — without a held-out calibration set we don't
know what an "absolute" 0.7 means for IsolationForest on this data.
"""
from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("wfm.anomaly")

ROLLING_WINDOW = 6  # 30-min intervals = 3 hours
DRIFT_Z_THRESHOLD = 1.5
SPIKE_CONTAMINATION = 0.05  # IF/LOF tuning param — assume ~5% are anomalous

# Phase 8 stage 5 — skill_mix_drift thresholds.
# χ²-distance between recent and baseline skill mix (both normalized to sum=1).
# Threshold of 0.05 flags meaningful shifts without firing on noise; tuned
# against the synthetic data that injects a month-end mix shift.
SKILL_MIX_DRIFT_THRESHOLD = 0.05
SKILL_MIX_BASELINE_DAYS = 28
SKILL_MIX_RECENT_DAYS = 3


# --------------------------------------------------------------------------
# Pure data
# --------------------------------------------------------------------------
@dataclass
class IntervalRow:
    interval_start: datetime
    queue: str
    observed: float
    expected: float

    @property
    def residual(self) -> float:
        return self.observed - self.expected


@dataclass
class AnomalyRow:
    id: str
    date: date
    interval_start: datetime
    queue: str
    category: str
    severity: str
    score: float
    observed: float | None
    expected: float | None
    residual: float | None
    detector: str
    note: str | None = None


# --------------------------------------------------------------------------
# Hashing — stable, idempotent, citation-friendly id
# --------------------------------------------------------------------------
def anomaly_id(d: date, queue: str, interval_start: datetime, category: str) -> str:
    """SHA256 of `date|queue|interval_start|category`, first 16 hex chars.

    Collisions at 16 hex (64 bits) for a single deployment are vanishingly
    unlikely. The DB unique constraint catches them anyway (silent-failure
    gap #2).
    """
    iv_iso = interval_start.replace(tzinfo=timezone.utc).isoformat()
    payload = f"{d.isoformat()}|{queue}|{iv_iso}|{category}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Severity — per-run percentile of the detector's own scores
# --------------------------------------------------------------------------
def _severity_for(score: float, all_scores: np.ndarray) -> str:
    """Top 10% = high, top 30% = medium, else low.

    `all_scores` is the full set of POSITIVE-FLAGGED scores from the same
    detector in the same run.
    """
    if all_scores.size == 0:
        return "low"
    p70 = float(np.quantile(all_scores, 0.70))
    p90 = float(np.quantile(all_scores, 0.90))
    if score >= p90:
        return "high"
    if score >= p70:
        return "medium"
    return "low"


# --------------------------------------------------------------------------
# Detectors — each takes a list[IntervalRow], returns list[AnomalyRow]
# --------------------------------------------------------------------------
def detect_isolation_forest(rows: list[IntervalRow]) -> list[AnomalyRow]:
    if len(rows) < 10:
        return []
    from sklearn.ensemble import IsolationForest

    X = np.array([[r.residual, abs(r.residual)] for r in rows])
    model = IsolationForest(
        contamination=SPIKE_CONTAMINATION,
        random_state=42,
        n_estimators=100,
    )
    pred = model.fit_predict(X)
    raw_scores = -model.score_samples(X)  # higher = more anomalous
    flagged = [(r, raw_scores[i]) for i, r in enumerate(rows) if pred[i] == -1]
    if not flagged:
        return []
    score_arr = np.array([s for _, s in flagged])
    out = []
    for r, s in flagged:
        out.append(_make_anomaly(r, "volume_spike", float(s), score_arr, "isolation_forest"))
    return out


def detect_lof(rows: list[IntervalRow]) -> list[AnomalyRow]:
    if len(rows) < 20:
        return []
    from sklearn.neighbors import LocalOutlierFactor

    X = np.array([[r.residual, abs(r.residual)] for r in rows])
    model = LocalOutlierFactor(
        n_neighbors=min(20, len(rows) - 1),
        contamination=SPIKE_CONTAMINATION,
    )
    pred = model.fit_predict(X)
    raw_scores = -model.negative_outlier_factor_  # higher = more outlier-y
    flagged = [(r, raw_scores[i]) for i, r in enumerate(rows) if pred[i] == -1]
    if not flagged:
        return []
    score_arr = np.array([s for _, s in flagged])
    out = []
    for r, s in flagged:
        out.append(_make_anomaly(r, "volume_spike", float(s), score_arr, "lof"))
    return out


def detect_rolling_mean(rows: list[IntervalRow]) -> list[AnomalyRow]:
    """Forecast bias drift: rolling-mean residual far from zero.

    A single big residual is captured by isolation_forest. This catches the
    quieter failure: residuals consistently positive (or negative) for hours,
    indicating the forecast is biased.
    """
    if len(rows) < ROLLING_WINDOW + 1:
        return []
    residuals = np.array([r.residual for r in rows], dtype=float)

    rolling = np.full(len(residuals), np.nan)
    for i in range(ROLLING_WINDOW - 1, len(residuals)):
        rolling[i] = residuals[i - ROLLING_WINDOW + 1 : i + 1].mean()

    # z-score the rolling mean against the unbiased baseline (mean ~ 0,
    # std from the data itself). Tiny std would div-by-zero.
    std = float(np.std(residuals)) or 1.0
    z = np.abs(rolling) / std

    flagged: list[tuple[IntervalRow, float]] = []
    for i, r in enumerate(rows):
        if math.isnan(z[i]):
            continue
        if z[i] >= DRIFT_Z_THRESHOLD:
            flagged.append((r, float(z[i])))
    if not flagged:
        return []
    score_arr = np.array([s for _, s in flagged])
    out = []
    for r, s in flagged:
        anomaly = _make_anomaly(
            r, "forecast_bias_drift", s, score_arr, "rolling_mean"
        )
        anomaly.note = (
            f"Rolling {ROLLING_WINDOW}-interval mean residual is "
            f"{s:.1f}σ from zero."
        )
        out.append(anomaly)
    return out


def skill_mix_chi_squared(
    baseline: dict[str, float], recent: dict[str, float]
) -> float:
    """Pearson χ² distance between two skill-mix distributions.

    Both inputs are skill_id (or skill_name) → volume. Internally normalized
    to sum=1 before comparison. Returns 0.0 when the two are identical;
    larger values indicate bigger shifts. The detector flags anomalies above
    SKILL_MIX_DRIFT_THRESHOLD.

    Why χ² and not KS: the data is categorical (3-7 skills, not a continuous
    distribution), and χ² is the textbook fit for categorical drift. KS would
    add weight without an obvious benefit on this scale.
    """
    keys = set(baseline) | set(recent)
    if not keys:
        return 0.0
    base_sum = sum(baseline.values()) or 1.0
    recent_sum = sum(recent.values()) or 1.0
    score = 0.0
    for k in keys:
        b = baseline.get(k, 0.0) / base_sum
        r = recent.get(k, 0.0) / recent_sum
        # Pearson form: (observed - expected)^2 / expected.
        # +1e-9 protects against a baseline-zero category (unseen-before
        # skill that just appeared); we still want a finite score there.
        score += (r - b) ** 2 / (b + 1e-9)
    return float(score)


def _make_anomaly(
    r: IntervalRow,
    category: str,
    score: float,
    score_set: np.ndarray,
    detector: str,
) -> AnomalyRow:
    d = r.interval_start.date()
    return AnomalyRow(
        id=anomaly_id(d, r.queue, r.interval_start, category),
        date=d,
        interval_start=r.interval_start,
        queue=r.queue,
        category=category,
        severity=_severity_for(score, score_set),
        score=float(score),
        observed=float(r.observed),
        expected=float(r.expected),
        residual=float(r.residual),
        detector=detector,
    )


# --------------------------------------------------------------------------
# Service — DB I/O around the math
# --------------------------------------------------------------------------
DETECTORS = {
    "isolation_forest": detect_isolation_forest,
    "lof": detect_lof,
    "rolling_mean": detect_rolling_mean,
}


class AnomalyService:
    def __init__(self, db: Session):
        self.db = db

    def detect(
        self,
        queue: str,
        start_date: date,
        end_date: date,
    ) -> tuple[int, int, list[str]]:
        """Run all three detectors over the residuals for (queue, [start, end]).

        Returns (inserted_count, skipped_duplicates, detectors_run).
        """
        rows = self._load_residuals(queue, start_date, end_date)
        if not rows:
            log.info("anomaly.detect: no residuals for %s %s..%s", queue, start_date, end_date)
            return 0, 0, []

        all_anomalies: list[AnomalyRow] = []
        ran: list[str] = []
        for name, fn in DETECTORS.items():
            try:
                found = fn(rows)
            except Exception:
                log.exception("Detector %s failed on %s — skipping", name, queue)
                continue
            ran.append(name)
            all_anomalies.extend(found)

        if not all_anomalies:
            return 0, 0, ran

        # Dedup within the same run by id (same interval can be flagged by
        # multiple detectors — keep the highest-severity record).
        deduped = self._dedup_by_id(all_anomalies)
        inserted, skipped = self._upsert(deduped)
        return inserted, skipped, ran

    def _load_residuals(
        self, queue: str, start_date: date, end_date: date
    ) -> list[IntervalRow]:
        """Pull (forecast, actual) pairs as IntervalRow.

        Joins the latest completed forecast for `queue` with interval_history
        on (interval_start). Records without a forecast value are dropped
        (can't compute a residual).
        """
        run_id = self.db.execute(
            text(
                """
                SELECT id FROM forecast_runs
                WHERE queue = :queue AND status = 'completed'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"queue": queue},
        ).scalar_one_or_none()
        if run_id is None:
            return []

        start = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)

        rows = (
            self.db.execute(
                text(
                    """
                    SELECT h.interval_start, h.queue, h.offered, f.forecast_offered
                    FROM interval_history h
                    JOIN forecast_intervals f
                      ON f.forecast_run_id = :run_id
                     AND f.interval_start = h.interval_start
                    WHERE h.queue = :queue
                      AND h.interval_start >= :start AND h.interval_start < :end
                    ORDER BY h.interval_start
                    """
                ),
                {
                    "run_id": run_id,
                    "queue": queue,
                    "start": start,
                    "end": end,
                },
            )
            .mappings()
            .all()
        )

        return [
            IntervalRow(
                interval_start=r["interval_start"],
                queue=r["queue"],
                observed=float(r["offered"] or 0),
                expected=float(r["forecast_offered"] or 0),
            )
            for r in rows
        ]

    @staticmethod
    def _dedup_by_id(items: Iterable[AnomalyRow]) -> list[AnomalyRow]:
        """Same id from two detectors — keep the one with higher severity, then higher score."""
        order = {"low": 0, "medium": 1, "high": 2}
        kept: dict[str, AnomalyRow] = {}
        for a in items:
            existing = kept.get(a.id)
            if existing is None:
                kept[a.id] = a
                continue
            if (order[a.severity], a.score) > (order[existing.severity], existing.score):
                kept[a.id] = a
        return list(kept.values())

    def _upsert(self, anomalies: list[AnomalyRow]) -> tuple[int, int]:
        """Insert with ON CONFLICT DO NOTHING. The conflict count is the
        skipped-duplicate signal that surfaces if a hash collides."""
        inserted = 0
        skipped = 0
        for a in anomalies:
            result = self.db.execute(
                text(
                    """
                    INSERT INTO anomalies
                        (id, date, interval_start, queue, category, severity,
                         score, observed, expected, residual, detector, note)
                    VALUES
                        (:id, :date, :interval_start, :queue, :category, :severity,
                         :score, :observed, :expected, :residual, :detector, :note)
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                {
                    "id": a.id,
                    "date": a.date,
                    "interval_start": a.interval_start,
                    "queue": a.queue,
                    "category": a.category,
                    "severity": a.severity,
                    "score": a.score,
                    "observed": a.observed,
                    "expected": a.expected,
                    "residual": a.residual,
                    "detector": a.detector,
                    "note": a.note,
                },
            )
            if result.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        self.db.commit()
        return inserted, skipped

    def detect_skill_mix_drift(
        self,
        queue: str,
        target_date: date,
        *,
        threshold: float = SKILL_MIX_DRIFT_THRESHOLD,
        baseline_days: int = SKILL_MIX_BASELINE_DAYS,
        recent_days: int = SKILL_MIX_RECENT_DAYS,
    ) -> tuple[int, int, float]:
        """Phase 8 stage 5 — detect skill_mix_drift via χ² distance.

        Compares skill mix over `recent_days` ending at target_date against
        `baseline_days` ending one day before the recent window starts. Inserts
        a single anomaly row when distance > threshold; the row is dated
        target_date and pinned to the queue.

        Returns (inserted, skipped, score). Score is the χ² distance regardless
        of whether it crossed threshold — useful for diagnostics.

        category="skill_mix_drift" (open enum, new for Phase 8 stage 5).
        detector="rolling_mean" (closed enum; this is an aggregation over
        a window, the closest fit semantically). The .note field carries the
        breakdown so chat-side citations have the receipts.
        """
        recent_end = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
        recent_start = recent_end - timedelta(days=recent_days)
        baseline_end = recent_start
        baseline_start = baseline_end - timedelta(days=baseline_days)

        baseline = self._load_skill_mix(queue, baseline_start, baseline_end)
        recent = self._load_skill_mix(queue, recent_start, recent_end)

        if not baseline or not recent:
            log.info(
                "skill_mix_drift: missing data for %s (baseline=%d skills, recent=%d skills)",
                queue, len(baseline), len(recent),
            )
            return 0, 0, 0.0

        score = skill_mix_chi_squared(baseline, recent)
        if score < threshold:
            return 0, 0, score

        # Build the .note diff payload — readable summary of which skills moved.
        base_sum = sum(baseline.values()) or 1.0
        recent_sum = sum(recent.values()) or 1.0
        moves = []
        for skill in sorted(set(baseline) | set(recent)):
            b = baseline.get(skill, 0.0) / base_sum
            r = recent.get(skill, 0.0) / recent_sum
            if abs(r - b) >= 0.02:
                moves.append(f"{skill}: {b * 100:.0f}% → {r * 100:.0f}%")
        note = (
            f"χ²={score:.3f} over {recent_days}d vs {baseline_days}d baseline. "
            + ("; ".join(moves) if moves else "Distribution shift across skills.")
        )

        # Severity bucketing — score is unbounded so use multiplicative bands.
        if score >= threshold * 4:
            severity = "high"
        elif score >= threshold * 2:
            severity = "medium"
        else:
            severity = "low"

        anomaly = AnomalyRow(
            id=anomaly_id(target_date, queue, recent_end, "skill_mix_drift"),
            date=target_date,
            interval_start=recent_end,
            queue=queue,
            category="skill_mix_drift",
            severity=severity,
            score=score,
            observed=None,
            expected=None,
            residual=None,
            detector="rolling_mean",
            note=note,
        )
        inserted, skipped = self._upsert([anomaly])
        return inserted, skipped, score

    def _load_skill_mix(
        self, queue: str, start: datetime, end: datetime
    ) -> dict[str, float]:
        """Sum of `offered` per skill_name within [start, end). Skills with
        zero rows are absent — caller normalizes."""
        rows = self.db.execute(
            text(
                """
                SELECT s.name AS skill, COALESCE(SUM(h.offered), 0) AS total
                FROM interval_history h
                JOIN skills s ON s.id = h.skill_id
                WHERE h.queue = :queue
                  AND h.skill_id IS NOT NULL
                  AND h.interval_start >= :start
                  AND h.interval_start <  :end
                GROUP BY s.name
                """
            ),
            {"queue": queue, "start": start, "end": end},
        ).all()
        return {r[0]: float(r[1]) for r in rows}

    def list(
        self,
        since: date,
        queue: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "WHERE date >= :since"
        params: dict[str, Any] = {"since": since, "limit": limit}
        if queue:
            where += " AND queue = :queue"
            params["queue"] = queue
        rows = (
            self.db.execute(
                text(
                    f"""
                    SELECT id, date, interval_start, queue, category, severity,
                           score, observed, expected, residual, detector, note
                    FROM anomalies
                    {where}
                    ORDER BY date DESC, severity DESC, score DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]
