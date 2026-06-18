"""
get_long_range_forecast tool — "1 + N" monthly capacity forecast.

Answers "what do the next ~9 months look like?" from the recent actuals we
have, at MONTHLY grain (not the interval-level get_forecast). Returns a
chart.line with an 'Actual' series (the seed month(s)) and a 'Forecast' series
that anchors on the last actual month so the two lines connect.

The projection is transparent arithmetic (see services/long_range_forecast.py),
not a fitted seasonal model — one month of data can't yield yearly seasonality,
so growth is an explicit, user-supplied assumption defaulting to flat.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.long_range_forecast import (
    DEFAULT_HORIZON_MONTHS,
    DEFAULT_SEED_MONTHS,
    NoHistoryError,
    build_long_range_forecast,
)

definition: dict[str, Any] = {
    "name": "get_long_range_forecast",
    "description": (
        "Long-range capacity forecast: project the next several MONTHS of "
        "contact volume from recent actuals, at monthly grain. Use for "
        "capacity-planning / headcount questions like 'forecast the next 9 "
        "months', 'what does the rest of the year look like', or 'how much "
        "volume should we plan for'. This is monthly and long-horizon — for a "
        "single day's interval-level forecast use get_forecast instead. "
        "Returns a line chart with an Actual series (the seed month) and a "
        "Forecast series. Growth is an explicit assumption (default flat); pass "
        "growth_rate_pct when the user states a month-over-month growth rate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queue": {
                "type": "string",
                "description": "Queue name. Real queues: 'all' (aggregate) or 'skills' (per-skill).",
            },
            "skill": {
                "type": "string",
                "description": "Optional skill name (only with queue='skills'), e.g. 'sales'.",
            },
            "horizon_months": {
                "type": "integer",
                "description": "Months to forecast forward. Defaults to 9 (the '1+9' default).",
            },
            "seed_months": {
                "type": "integer",
                "description": "Months of recent actuals to seed the baseline. Defaults to 1.",
            },
            "growth_rate_pct": {
                "type": "number",
                "description": (
                    "Assumed month-over-month growth, in percent (e.g. 5 = +5%/mo, "
                    "-2 = -2%/mo). Defaults to 0 (flat). Compounds across the horizon."
                ),
            },
        },
        "required": ["queue"],
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    queue: str = args["queue"]
    channel: str = args.get("channel", "voice")
    skill_name: str | None = args.get("skill")
    horizon_months = _coerce_int(args.get("horizon_months"), DEFAULT_HORIZON_MONTHS)
    seed_months = _coerce_int(args.get("seed_months"), DEFAULT_SEED_MONTHS)
    growth_rate_monthly = _coerce_float(args.get("growth_rate_pct"), 0.0) / 100.0

    skill_id: int | None = None
    if skill_name:
        skill_id = db.execute(
            text("SELECT id FROM skills WHERE name = :name"),
            {"name": skill_name},
        ).scalar_one_or_none()
        if skill_id is None:
            return {
                "render": "error",
                "message": f"Unknown skill {skill_name!r}.",
                "code": "UNKNOWN_SKILL",
            }

    try:
        result = build_long_range_forecast(
            db,
            queue=queue,
            channel=channel,
            skill_id=skill_id,
            skill=skill_name,
            seed_months=seed_months,
            horizon_months=horizon_months,
            growth_rate_monthly=growth_rate_monthly,
        )
    except NoHistoryError:
        return {
            "render": "error",
            "message": (
                f"No history to forecast from for queue {queue!r}"
                + (f", skill {skill_name!r}" if skill_name else "")
                + "."
            ),
            "code": "FORECAST_NOT_FOUND",
        }

    label = skill_name or queue
    actual_points = [{"x": p.ym, "y": p.offered} for p in result.seed]
    # Anchor the forecast on the last actual month so the lines join cleanly.
    forecast_points = []
    if result.seed:
        last = result.seed[-1]
        forecast_points.append({"x": last.ym, "y": last.offered})
    forecast_points += [{"x": p.ym, "y": p.offered} for p in result.forecast]

    peak_fte = max((p.implied_fte for p in result.forecast), default=0.0)
    growth_note = (
        f" · {growth_rate_monthly * 100:+.0f}%/mo"
        if growth_rate_monthly
        else " · flat"
    )
    title = (
        f"Long-range forecast — {label} · {result.seed_months} mo actual "
        f"→ {result.horizon_months} mo{growth_note} · "
        f"AHT≈{result.seed_aht_seconds:.0f}s, peak≈{peak_fte:.0f} FTE"
    )

    return {
        "render": "chart.line",
        "title": title,
        "yLabel": "contacts/mo",
        "series": [
            {"name": "Actual", "points": actual_points},
            {"name": "Forecast", "points": forecast_points},
        ],
    }


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
