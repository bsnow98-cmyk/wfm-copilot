"""
ForecastService — wraps Nixtla's statsforecast and the DB.

Pipeline for one forecast run:
    1. load history for (queue, channel) from interval_history
    2. reindex onto a regular 30-min grid, filling closed-hour gaps with 0
    3. (optional) backtest: hold out last `backtest_days`, fit, predict, score MAPE/WAPE
    4. refit on the full history
    5. predict `horizon_days * 48` intervals into the future
    6. write forecast_intervals + update forecast_runs.status -> 'completed'

Models:
    seasonal_naive — copies the last weekly pattern. Useful as a sanity baseline.
    auto_arima     — SARIMA with weekly seasonality (336 half-hours).
    mstl           — multi-seasonal (daily 48 + weekly 336). Default — best fit
                     for contact-center 30-min data with both intraday and
                     weekly patterns.

Why MSTL: contact center volume has a strong daily curve AND a strong weekly
pattern. AutoARIMA can only model one seasonality at a time; MSTL decomposes
both and forecasts the residual with AutoARIMA.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session


def _nan_to_none(x: float | None) -> float | None:
    """Postgres NUMERIC accepts NaN, but JSON serialization later chokes on it.
    Normalize NaN -> None up front."""
    if x is None:
        return None
    try:
        if math.isnan(x):
            return None
    except (TypeError, ValueError):
        return None
    return x

log = logging.getLogger("wfm.forecast")

# 30-min interval cadence assumed throughout. If you support other cadences
# later, lift these to per-run config.
INTERVAL_FREQ = "30min"
DAILY_PERIODS = 48      # 30-min intervals per day
WEEKLY_PERIODS = 336    # 30-min intervals per week


# --------------------------------------------------------------------------
# Model factory — kept tiny on purpose. To add a model, add a key here.
# --------------------------------------------------------------------------
def _build_model(name: str):
    # Lazy import so the API can boot even if statsforecast isn't installed yet.
    from statsforecast.models import AutoARIMA, MSTL, SeasonalNaive

    if name == "seasonal_naive":
        return SeasonalNaive(season_length=WEEKLY_PERIODS)
    if name == "auto_arima":
        return AutoARIMA(season_length=WEEKLY_PERIODS, max_p=2, max_q=2, max_P=1, max_Q=1)
    if name == "mstl":
        return MSTL(
            season_length=[DAILY_PERIODS, WEEKLY_PERIODS],
            trend_forecaster=AutoARIMA(max_p=2, max_q=2),
        )
    raise ValueError(f"Unknown model: {name}")


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------
class ForecastService:
    def __init__(self, db: Session):
        self.db = db

    # ----- public API ---------------------------------------------------
    def create_run(
        self,
        queue: str,
        channel: str,
        horizon_days: int,
        model: str,
        backtest_days: int,
        skill_id: int | None = None,
    ) -> int:
        """Insert a forecast_runs row in 'pending' state and return its id.

        The actual training/forecasting happens in execute_run(), called from
        a FastAPI BackgroundTask.

        Phase 8 — when `skill_id` is set, this run trains on
        interval_history filtered to that skill_id. Aggregate runs (skill_id
        IS NULL) keep the Phase 2 behavior of training on all rows for the
        queue regardless of skill.
        """
        row = self.db.execute(
            text("""
                INSERT INTO forecast_runs
                    (queue, channel, model_name, horizon_start, horizon_end,
                     status, skill_id)
                VALUES
                    (:queue, :channel, :model, :h_start, :h_end, 'pending',
                     :skill_id)
                RETURNING id
            """),
            {
                "queue": queue,
                "channel": channel,
                "model": model,
                # Placeholders — filled with real values once we know the
                # last interval in history.
                "h_start": datetime.now(timezone.utc),
                "h_end": datetime.now(timezone.utc) + timedelta(days=horizon_days),
                "skill_id": skill_id,
            },
        ).fetchone()
        self.db.commit()
        return int(row[0])

    def execute_run(
        self,
        run_id: int,
        queue: str,
        channel: str,
        horizon_days: int,
        model: str,
        backtest_days: int,
        skill_id: int | None = None,
    ) -> None:
        """Train + predict + persist. Catches everything and writes status."""
        try:
            self._mark_running(run_id)

            df = self._load_history(queue, channel, skill_id=skill_id)
            if df.empty:
                raise ValueError(
                    f"No interval_history rows for queue={queue!r} channel={channel!r}"
                )
            log.info("[run %s] loaded %d history rows", run_id, len(df))

            df = self._reindex_full_grid(df)
            log.info("[run %s] reindexed to %d intervals", run_id, len(df))

            # Backtest on holdout (optional).
            mape, wape = (None, None)
            if backtest_days > 0:
                holdout = backtest_days * DAILY_PERIODS
                if len(df) > holdout + WEEKLY_PERIODS * 2:
                    mape, wape = self._backtest(df, model, holdout)
                    log.info("[run %s] backtest mape=%.3f wape=%.3f", run_id, mape, wape)
                else:
                    log.warning(
                        "[run %s] not enough history for backtest (need >%d rows, have %d)",
                        run_id, holdout + WEEKLY_PERIODS * 2, len(df),
                    )

            # Refit on full history and forecast.
            horizon_periods = horizon_days * DAILY_PERIODS
            forecast_df = self._fit_and_predict(df, model, horizon_periods)
            log.info("[run %s] forecast produced %d intervals", run_id, len(forecast_df))

            # Persist.
            self._write_intervals(run_id, forecast_df)
            self._mark_completed(
                run_id,
                horizon_start=forecast_df["ds"].min(),
                horizon_end=forecast_df["ds"].max(),
                mape=_nan_to_none(mape),
                wape=_nan_to_none(wape),
            )
            log.info("[run %s] completed", run_id)

        except Exception as exc:  # noqa: BLE001
            log.exception("[run %s] failed", run_id)
            self._mark_failed(run_id, str(exc))

    # ----- internals ----------------------------------------------------
    def _mark_running(self, run_id: int) -> None:
        self.db.execute(
            text("UPDATE forecast_runs SET status='running', started_at=NOW() WHERE id=:id"),
            {"id": run_id},
        )
        self.db.commit()

    def _mark_completed(
        self,
        run_id: int,
        horizon_start: datetime,
        horizon_end: datetime,
        mape: float | None,
        wape: float | None,
    ) -> None:
        self.db.execute(
            text("""
                UPDATE forecast_runs
                SET status='completed',
                    completed_at=NOW(),
                    horizon_start=:h_start,
                    horizon_end=:h_end,
                    mape=:mape,
                    wape=:wape
                WHERE id=:id
            """),
            {
                "id": run_id,
                "h_start": horizon_start,
                "h_end": horizon_end,
                "mape": mape,
                "wape": wape,
            },
        )
        self.db.commit()

    def _mark_failed(self, run_id: int, msg: str) -> None:
        self.db.execute(
            text("""
                UPDATE forecast_runs
                SET status='failed', completed_at=NOW(), error_message=:msg
                WHERE id=:id
            """),
            {"id": run_id, "msg": msg[:1000]},
        )
        self.db.commit()

    def _load_history(
        self, queue: str, channel: str, skill_id: int | None = None
    ) -> pd.DataFrame:
        # Phase 8 — when skill_id is set, filter to that skill. When NULL we
        # want all rows for the queue regardless of any skill_id values
        # (aggregate behavior). The two CASE branches keep this honest:
        # `:skill_id IS NULL` short-circuits the skill filter for aggregate
        # runs without breaking on tables that have no skill_id column on
        # pre-Phase-8 deployments — the column existence is checked by the
        # migration runner.
        rows = self.db.execute(
            text("""
                SELECT interval_start, offered, aht_seconds
                FROM interval_history
                WHERE queue=:queue
                  AND channel=:channel
                  AND (:skill_id IS NULL OR skill_id = :skill_id)
                ORDER BY interval_start
            """),
            {"queue": queue, "channel": channel, "skill_id": skill_id},
        ).fetchall()
        if not rows:
            return pd.DataFrame(columns=["ds", "y", "aht"])

        df = pd.DataFrame(rows, columns=["ds", "y", "aht"])
        # statsforecast wants tz-naive datetimes with a regular freq.
        df["ds"] = pd.to_datetime(df["ds"], utc=True).dt.tz_convert(None)
        df["y"] = pd.to_numeric(df["y"]).astype(float)
        df["aht"] = pd.to_numeric(df["aht"]).astype(float)
        return df

    def _reindex_full_grid(self, df: pd.DataFrame) -> pd.DataFrame:
        """Fill closed-hour gaps with 0 so the series has a regular cadence."""
        if df.empty:
            return df
        full_idx = pd.date_range(
            start=df["ds"].min(),
            end=df["ds"].max(),
            freq=INTERVAL_FREQ,
        )
        out = (
            df.set_index("ds")
              .reindex(full_idx)
              .rename_axis("ds")
              .reset_index()
        )
        out["y"] = out["y"].fillna(0.0)
        out["aht"] = out["aht"].ffill().bfill().fillna(0.0)
        out["unique_id"] = "series"  # statsforecast requires this column
        return out

    def _backtest(self, df: pd.DataFrame, model_name: str, holdout: int) -> tuple[float, float]:
        from statsforecast import StatsForecast

        train = df.iloc[:-holdout].copy()
        test = df.iloc[-holdout:].copy()

        sf = StatsForecast(models=[_build_model(model_name)], freq=INTERVAL_FREQ, n_jobs=1)
        sf.fit(train[["unique_id", "ds", "y"]])
        fc = sf.predict(h=holdout)

        # statsforecast names the prediction column after the model class.
        pred_col = [c for c in fc.columns if c not in ("unique_id", "ds")][0]
        merged = test.merge(fc, on=["unique_id", "ds"], how="inner")
        if merged.empty:
            return float("nan"), float("nan")

        actual = merged["y"].astype(float)
        pred = merged[pred_col].astype(float).clip(lower=0)

        # MAPE blows up on zero actuals. Compute on intervals where actual > 0.
        nonzero = actual > 0
        mape = float(((actual[nonzero] - pred[nonzero]).abs() / actual[nonzero]).mean()) if nonzero.any() else float("nan")
        wape = float((actual - pred).abs().sum() / max(actual.sum(), 1.0))
        return mape, wape

    def _fit_and_predict(self, df: pd.DataFrame, model_name: str, horizon: int) -> pd.DataFrame:
        from statsforecast import StatsForecast

        sf = StatsForecast(models=[_build_model(model_name)], freq=INTERVAL_FREQ, n_jobs=1)
        sf.fit(df[["unique_id", "ds", "y"]])
        fc = sf.predict(h=horizon)
        pred_col = [c for c in fc.columns if c not in ("unique_id", "ds")][0]

        # Forecast AHT as the trailing 30-day mean per time-of-day.
        # Simple baseline that's plenty good for Phase 2; revisit later.
        recent = df.tail(30 * DAILY_PERIODS).copy()
        recent["tod"] = recent["ds"].dt.hour * 60 + recent["ds"].dt.minute
        aht_by_tod = recent.groupby("tod")["aht"].mean().to_dict()

        out = pd.DataFrame({
            "ds": fc["ds"].values,
            "forecast_offered": np.maximum(fc[pred_col].astype(float).values, 0.0),
        })
        tod = pd.to_datetime(out["ds"]).dt.hour * 60 + pd.to_datetime(out["ds"]).dt.minute
        out["forecast_aht_seconds"] = tod.map(aht_by_tod).fillna(recent["aht"].mean())
        return out

    def _write_intervals(self, run_id: int, forecast_df: pd.DataFrame) -> None:
        rows = [
            {
                "run_id": run_id,
                "ds": pd.Timestamp(r["ds"]).to_pydatetime().replace(tzinfo=timezone.utc),
                "offered": float(r["forecast_offered"]),
                "aht": float(r["forecast_aht_seconds"]),
            }
            for _, r in forecast_df.iterrows()
        ]
        self.db.execute(
            text("""
                INSERT INTO forecast_intervals
                    (forecast_run_id, interval_start, forecast_offered, forecast_aht_seconds)
                VALUES (:run_id, :ds, :offered, :aht)
                ON CONFLICT (forecast_run_id, interval_start) DO UPDATE SET
                    forecast_offered = EXCLUDED.forecast_offered,
                    forecast_aht_seconds = EXCLUDED.forecast_aht_seconds
            """),
            rows,
        )
        self.db.commit()
