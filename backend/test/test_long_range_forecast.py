"""
Unit tests for the 1+N long-range monthly forecast.

These mock the DB rollup query so they run without Postgres — they pin the
projection arithmetic (month-length scaling, growth compounding, demand-weighted
AHT, implied-FTE) and the tool's chart.line render shape.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.services.long_range_forecast import (
    NoHistoryError,
    build_long_range_forecast,
)
from app.tools import dispatch
from app.tools.get_long_range_forecast import handler


def _db_with_rollup(rows):
    """A Session mock whose execute(...).all() returns `rows` and whose
    execute(...).scalar_one_or_none() returns None (no skill lookup)."""
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    return db


# One seed month: June 2026 (30 days), 30k contacts, AHT 300s, full coverage.
SEED_JUNE = [(datetime(2026, 6, 1), 30000, 30000 * 300, 30)]


def test_flat_growth_scales_by_month_length() -> None:
    db = _db_with_rollup(SEED_JUNE)
    fc = build_long_range_forecast(db, queue="all", horizon_months=9)

    assert fc.seed_months == 1
    assert fc.baseline_daily_offered == 1000.0  # 30000 / 30 active days
    assert fc.seed_aht_seconds == 300.0
    assert len(fc.forecast) == 9
    assert fc.forecast[0].ym == "2026-07"
    assert fc.forecast[-1].ym == "2027-03"

    # Flat growth → volume tracks calendar days. July=31, Feb 2027=28.
    july = fc.forecast[0]
    assert july.offered == pytest.approx(31000.0)  # 1000 * 31
    feb = next(p for p in fc.forecast if p.ym == "2027-02")
    assert feb.offered == pytest.approx(28000.0)  # 1000 * 28


def test_implied_fte_math() -> None:
    db = _db_with_rollup(SEED_JUNE)
    fc = build_long_range_forecast(db, queue="all", horizon_months=1)
    july = fc.forecast[0]
    # 31000 contacts * 300s / 3600 = 2583.33 handling hrs;
    # capacity per FTE = 160 * 0.85 = 136 hrs -> ~19 FTE.
    assert july.implied_fte == pytest.approx(19.0, abs=0.1)


def test_growth_compounds() -> None:
    db = _db_with_rollup(SEED_JUNE)
    fc = build_long_range_forecast(
        db, queue="all", horizon_months=2, growth_rate_monthly=0.10
    )
    # July (k=1): 1000*31*1.10 = 34100. Aug (k=2): 1000*31*1.21 = 37510.
    assert fc.forecast[0].offered == pytest.approx(34100.0)
    assert fc.forecast[1].offered == pytest.approx(37510.0)


def test_partial_seed_month_normalizes_by_active_days() -> None:
    # Only 15 days of data in June, 15k contacts -> baseline 1000/day, not 500.
    db = _db_with_rollup([(datetime(2026, 6, 1), 15000, 15000 * 300, 15)])
    fc = build_long_range_forecast(db, queue="all", horizon_months=1)
    assert fc.baseline_daily_offered == 1000.0
    assert fc.forecast[0].offered == pytest.approx(31000.0)  # July, 31 days


def test_demand_weighted_aht_across_seed_window() -> None:
    # Two seed months with different AHT; weight by volume, not a flat mean.
    rows = [
        (datetime(2026, 5, 1), 10000, 10000 * 200, 31),  # May: AHT 200
        (datetime(2026, 6, 1), 30000, 30000 * 400, 30),  # June: AHT 400
    ]
    db = _db_with_rollup(rows)
    fc = build_long_range_forecast(db, queue="all", seed_months=2, horizon_months=1)
    # Weighted: (10000*200 + 30000*400) / 40000 = 350, not (200+400)/2=300.
    assert fc.seed_aht_seconds == pytest.approx(350.0)


def test_no_history_raises() -> None:
    db = _db_with_rollup([])
    with pytest.raises(NoHistoryError):
        build_long_range_forecast(db, queue="ghost")


def test_handler_returns_chart_line_with_month_axis() -> None:
    db = _db_with_rollup(SEED_JUNE)
    out = handler({"queue": "all"}, db)
    assert out["render"] == "chart.line"
    names = {s["name"] for s in out["series"]}
    assert names == {"Actual", "Forecast"}
    forecast = next(s for s in out["series"] if s["name"] == "Forecast")
    # Anchored on the last actual month, then 9 forecast months = 10 points.
    assert len(forecast["points"]) == 10
    assert forecast["points"][0]["x"] == "2026-06"  # anchor
    assert all(len(p["x"]) == 7 and p["x"][4] == "-" for p in forecast["points"])


def test_handler_unknown_skill_is_typed_error() -> None:
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None  # skill not found
    db.execute.return_value = result
    out = handler({"queue": "skills", "skill": "nope"}, db)
    assert out["render"] == "error"
    assert out["code"] == "UNKNOWN_SKILL"


def test_dispatch_routes_to_tool() -> None:
    db = _db_with_rollup(SEED_JUNE)
    out = dispatch("get_long_range_forecast", {"queue": "all"}, db)
    assert out["render"] == "chart.line"
