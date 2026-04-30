"""
Phase 8 Stage 5 — skill_mix_drift detection.

Two layers:
- skill_mix_chi_squared (pure math): identity case, symmetric input handling,
  monotone in actual shift size.
- detect_skill_mix_drift end-to-end: would need a real DB. We test the
  upstream math + boundary behaviour, leaving the DB-bound integration to
  the eval suite that runs against a live Postgres.
"""
from __future__ import annotations

import pytest

from app.services.anomaly import (
    SKILL_MIX_DRIFT_THRESHOLD,
    skill_mix_chi_squared,
)


def test_identical_distributions_score_zero() -> None:
    base = {"sales": 30.0, "support": 55.0, "billing": 15.0}
    recent = dict(base)
    assert skill_mix_chi_squared(base, recent) == pytest.approx(0.0, abs=1e-9)


def test_score_grows_with_shift_size() -> None:
    base = {"sales": 30.0, "support": 55.0, "billing": 15.0}
    small_shift = {"sales": 32.0, "support": 53.0, "billing": 15.0}
    big_shift = {"sales": 45.0, "support": 40.0, "billing": 15.0}
    s_small = skill_mix_chi_squared(base, small_shift)
    s_big = skill_mix_chi_squared(base, big_shift)
    assert 0.0 < s_small < s_big


def test_unit_scaling_invariance() -> None:
    """Same proportions, different absolute volumes — score should match (or
    be very close, modulo floating-point) since both inputs are normalized."""
    base = {"sales": 30.0, "support": 55.0, "billing": 15.0}
    recent_scaled = {"sales": 300.0, "support": 550.0, "billing": 150.0}
    assert skill_mix_chi_squared(base, recent_scaled) == pytest.approx(0.0, abs=1e-9)


def test_new_skill_in_recent_returns_finite_score() -> None:
    """A skill that wasn't in baseline shouldn't blow up to infinity. The
    +1e-9 epsilon in the helper guards against div-by-zero for
    previously-unseen categories."""
    base = {"sales": 30.0, "support": 55.0}
    recent = {"sales": 25.0, "support": 50.0, "billing": 25.0}
    score = skill_mix_chi_squared(base, recent)
    import math

    assert math.isfinite(score)
    assert score > 0.0


def test_threshold_value_is_meaningful() -> None:
    """A baseline 30/55/15 vs a meaningfully shifted 45/40/15 should
    cross the configured threshold; a tiny 32/53/15 shift should not.
    This test pins the threshold semantically — if we ever bump it, the
    tradeoff with detection sensitivity is visible here."""
    base = {"sales": 30.0, "support": 55.0, "billing": 15.0}
    small = {"sales": 32.0, "support": 53.0, "billing": 15.0}
    big = {"sales": 45.0, "support": 40.0, "billing": 15.0}

    assert skill_mix_chi_squared(base, small) < SKILL_MIX_DRIFT_THRESHOLD
    assert skill_mix_chi_squared(base, big) >= SKILL_MIX_DRIFT_THRESHOLD


def test_empty_inputs_return_zero() -> None:
    assert skill_mix_chi_squared({}, {}) == 0.0
    assert skill_mix_chi_squared({"sales": 0.0}, {"sales": 0.0}) == 0.0
