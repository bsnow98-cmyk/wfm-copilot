"""
LongRangeForecastService — a "1 + N" capacity-planning forecast.

Where ForecastService (services/forecasting.py) fits MSTL/ARIMA on a 30-min
grid to predict the next few *days* at interval grain, this service answers a
different question: "given the volume we just lived through, what does the next
~9 *months* look like for capacity planning?"

It is deliberately NOT a statistical seasonal model. With as little as one month
of real data you cannot estimate yearly seasonality from the data itself, and
pretending otherwise would be dishonest. So the projection is transparent
arithmetic the user can audit:

    baseline_daily   = total seed contacts / distinct days that actually had data
    month_contacts   = baseline_daily * days_in_month
                       * (1 + growth_rate_monthly) ** k        # k = 1..horizon
                       * seasonal_index[month]                  # default 1.0
    month_aht        = demand-weighted mean AHT of the seed window (held flat)
    implied_fte      = (month_contacts * month_aht / 3600)
                       / (agent_hours_per_month * target_occupancy)

`growth_rate_monthly` and `seasonality` are assumption knobs, not fitted values
— the caller supplies them (e.g. from the user's "assume 5% MoM growth"). The
month-length normalization is the one genuinely data-free signal that varies the
months even at flat growth: February is shorter than March.

Normalizing by *distinct days with data* (not calendar days) keeps a partial
seed month from understating the baseline — if we only have 18 days of June,
we divide June's volume by 18, not 30.
"""
from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("wfm.forecast.longrange")

DEFAULT_SEED_MONTHS = 1
DEFAULT_HORIZON_MONTHS = 9
DEFAULT_TARGET_OCCUPANCY = 0.85
# Productive hours one FTE delivers per month, net of shrinkage. ~37 hr/wk
# productive across ~4.33 weeks. A planning constant, overridable per call.
DEFAULT_AGENT_HOURS_PER_MONTH = 160.0


@dataclass
class MonthPoint:
    ym: str  # "YYYY-MM"
    offered: float
    aht_seconds: float
    implied_fte: float
    kind: str  # "actual" | "forecast"


@dataclass
class LongRangeForecast:
    queue: str
    channel: str
    skill: str | None
    seed_months: int
    horizon_months: int
    growth_rate_monthly: float
    seasonality_applied: bool
    target_occupancy: float
    agent_hours_per_month: float
    baseline_daily_offered: float
    seed_aht_seconds: float
    seed: list[MonthPoint] = field(default_factory=list)
    forecast: list[MonthPoint] = field(default_factory=list)


class NoHistoryError(ValueError):
    """Raised when there are no interval_history rows to seed the forecast."""


def _add_months(year: int, month: int, k: int) -> tuple[int, int]:
    """Return (year, month) k months after the given year/month (1-indexed)."""
    idx = (year * 12 + (month - 1)) + k
    return idx // 12, (idx % 12) + 1


def _implied_fte(
    offered: float,
    aht_seconds: float,
    agent_hours_per_month: float,
    target_occupancy: float,
) -> float:
    """Workload-over-occupancy FTE estimate.

    NOT Erlang C — Erlang needs an interval-level arrival rate and an SLA, both
    meaningless at monthly grain. This is the planning-grade approximation:
    total handling hours, inflated for the fact that agents can't be 100% busy.
    """
    capacity_per_fte = agent_hours_per_month * target_occupancy
    if capacity_per_fte <= 0:
        return 0.0
    workload_hours = offered * aht_seconds / 3600.0
    return workload_hours / capacity_per_fte


def build_long_range_forecast(
    db: Session,
    *,
    queue: str,
    channel: str = "voice",
    skill_id: int | None = None,
    skill: str | None = None,
    seed_months: int = DEFAULT_SEED_MONTHS,
    horizon_months: int = DEFAULT_HORIZON_MONTHS,
    growth_rate_monthly: float = 0.0,
    seasonality: dict[int, float] | None = None,
    target_occupancy: float = DEFAULT_TARGET_OCCUPANCY,
    agent_hours_per_month: float = DEFAULT_AGENT_HOURS_PER_MONTH,
) -> LongRangeForecast:
    """Build a monthly 1+N forecast from recent interval_history.

    `seed_months` most-recent months of actuals seed the baseline; the next
    `horizon_months` calendar months are projected. Raises NoHistoryError when
    the (queue, channel[, skill]) slice has no history.
    """
    seed_months = max(1, int(seed_months))
    horizon_months = max(1, int(horizon_months))

    # Monthly rollup. SUM(offered*aht)/SUM(offered) is the demand-weighted AHT;
    # COUNT(DISTINCT day) lets us normalize a partial seed month honestly.
    # CAST(:skill_id AS BIGINT) IS NULL mirrors forecasting.py — psycopg3 can't
    # infer the type of a bound NULL, so the cast is load-bearing, not cosmetic.
    rows = db.execute(
        text(
            """
            SELECT date_trunc('month', interval_start) AS m,
                   SUM(offered)                          AS offered,
                   SUM(offered * aht_seconds)            AS weighted_aht_num,
                   COUNT(DISTINCT CAST(interval_start AS date)) AS active_days
            FROM interval_history
            WHERE queue = :queue
              AND channel = :channel
              AND (CAST(:skill_id AS BIGINT) IS NULL
                   OR skill_id = CAST(:skill_id AS BIGINT))
            GROUP BY 1
            ORDER BY 1
            """
        ),
        {"queue": queue, "channel": channel, "skill_id": skill_id},
    ).all()

    if not rows:
        raise NoHistoryError(
            f"No interval_history for queue={queue!r} channel={channel!r} "
            f"skill_id={skill_id!r}"
        )

    # The most-recent `seed_months` months are the seed window.
    seed_rows = rows[-seed_months:]

    seed_offered_total = 0.0
    seed_aht_num_total = 0.0
    seed_active_days_total = 0
    seed_points: list[MonthPoint] = []
    for m, offered, waht_num, active_days in seed_rows:
        offered_f = float(offered or 0.0)
        waht_num_f = float(waht_num or 0.0)
        days = int(active_days or 0)
        seed_offered_total += offered_f
        seed_aht_num_total += waht_num_f
        seed_active_days_total += days
        month_aht = waht_num_f / offered_f if offered_f > 0 else 0.0
        d: date = m.date() if hasattr(m, "date") else m
        seed_points.append(
            MonthPoint(
                ym=f"{d.year:04d}-{d.month:02d}",
                offered=round(offered_f, 1),
                aht_seconds=round(month_aht, 1),
                implied_fte=round(
                    _implied_fte(offered_f, month_aht, agent_hours_per_month, target_occupancy),
                    1,
                ),
                kind="actual",
            )
        )

    if seed_active_days_total <= 0 or seed_offered_total <= 0:
        raise NoHistoryError(
            f"Seed window for queue={queue!r} has no usable volume "
            f"(offered={seed_offered_total}, active_days={seed_active_days_total})"
        )

    baseline_daily = seed_offered_total / seed_active_days_total
    seed_aht = seed_aht_num_total / seed_offered_total

    # Project forward from the last seed month.
    last_d: date = seed_rows[-1][0].date() if hasattr(seed_rows[-1][0], "date") else seed_rows[-1][0]
    forecast_points: list[MonthPoint] = []
    for k in range(1, horizon_months + 1):
        y, mo = _add_months(last_d.year, last_d.month, k)
        days_in_month = calendar.monthrange(y, mo)[1]
        growth_factor = (1.0 + growth_rate_monthly) ** k
        seasonal_index = (seasonality or {}).get(mo, 1.0)
        offered = baseline_daily * days_in_month * growth_factor * seasonal_index
        forecast_points.append(
            MonthPoint(
                ym=f"{y:04d}-{mo:02d}",
                offered=round(offered, 1),
                aht_seconds=round(seed_aht, 1),
                implied_fte=round(
                    _implied_fte(offered, seed_aht, agent_hours_per_month, target_occupancy),
                    1,
                ),
                kind="forecast",
            )
        )

    return LongRangeForecast(
        queue=queue,
        channel=channel,
        skill=skill,
        seed_months=len(seed_points),
        horizon_months=horizon_months,
        growth_rate_monthly=growth_rate_monthly,
        seasonality_applied=bool(seasonality),
        target_occupancy=target_occupancy,
        agent_hours_per_month=agent_hours_per_month,
        baseline_daily_offered=round(baseline_daily, 1),
        seed_aht_seconds=round(seed_aht, 1),
        seed=seed_points,
        forecast=forecast_points,
    )
