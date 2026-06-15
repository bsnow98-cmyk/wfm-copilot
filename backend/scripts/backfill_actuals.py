"""
Backfill realistic actuals so forecast-accuracy / SL-miss have data to score.

The synthetic seed generates forecasts for a forward horizon but actuals that
end before it, so forecast_intervals and interval_history never overlap on a
single day — get_forecast_accuracy and explain_sl_miss return NO_ACTUALS for
every queue and date. (Surfaced by the executive fill-test, 2026-06.)

This fills interval_history for the PAST portion of each completed forecast's
horizon — from the day after actuals currently end, up to (but not including)
sim-today. Future intervals are never touched: "today's actuals aren't in yet"
must stay true, and intraday's actual-so-far semantics depend on it.

Actuals are derived from the forecast with a deterministic per-interval error
(hash-based, ~±12%), so forecast-accuracy shows a believable, non-trivial MAPE
that is stable across re-runs. Idempotent: ON CONFLICT updates in place.

    DATABASE_URL=postgresql+psycopg://... python -m scripts.backfill_actuals
Local (no DATABASE_URL): falls back to app config (compose container).
"""
from __future__ import annotations

import hashlib
import os
from datetime import date

from sqlalchemy import create_engine, text


def _engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        from app.config import get_settings

        s = get_settings()
        url = (
            f"postgresql+psycopg://{s.postgres_user}:{s.postgres_password}"
            f"@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}"
        )
    return create_engine(url, future=True)


def _error_factor(run_id: int, interval_start) -> float:
    """Deterministic multiplier in ~[0.88, 1.12] from a stable hash."""
    key = f"{run_id}|{interval_start.isoformat()}".encode()
    h = int(hashlib.sha256(key).hexdigest()[:8], 16)
    return 0.88 + (h % 1000) / 1000.0 * 0.24  # 0.88 .. 1.12


def backfill() -> dict[str, int]:
    eng = _engine()
    with eng.begin() as conn:
        sim_today: date = conn.execute(text("SELECT sim_now()::date")).scalar_one()

        runs = conn.execute(
            text(
                "SELECT id, queue, skill_id FROM forecast_runs "
                "WHERE status = 'completed'"
            )
        ).mappings().all()

        total = 0
        per_run: dict[int, int] = {}
        for run in runs:
            rid, queue, skill_id = run["id"], run["queue"], run["skill_id"]
            rows = conn.execute(
                text(
                    """
                    SELECT interval_start, forecast_offered, forecast_aht_seconds
                    FROM forecast_intervals
                    WHERE forecast_run_id = :rid
                      AND interval_start::date < :sim_today
                    ORDER BY interval_start
                    """
                ),
                {"rid": rid, "sim_today": sim_today},
            ).mappings().all()

            n = 0
            for r in rows:
                ts = r["interval_start"]
                fc = float(r["forecast_offered"])
                aht = float(r["forecast_aht_seconds"] or 300.0)
                offered = max(0, round(fc * _error_factor(rid, ts)))
                # Realistic downstream metrics: most calls handled, a few abandon.
                abandoned = round(offered * 0.04)
                handled = max(0, offered - abandoned)
                # Service level wobbles with load vs forecast — under-forecast
                # days (actual > forecast) dip SL below target.
                load_ratio = (offered / fc) if fc > 0 else 1.0
                sl = max(0.55, min(0.97, 0.86 - (load_ratio - 1.0) * 1.5))
                asa = max(5.0, 18.0 + (load_ratio - 1.0) * 60.0)

                conn.execute(
                    text(
                        """
                        INSERT INTO interval_history
                            (queue, channel, interval_start, interval_minutes,
                             offered, handled, abandoned, aht_seconds,
                             asa_seconds, service_level, skill_id)
                        VALUES
                            (:queue, 'voice', :ts, 30, :offered, :handled,
                             :abandoned, :aht, :asa, :sl, :skill_id)
                        ON CONFLICT (queue, channel, interval_start, skill_id)
                        DO UPDATE SET
                            offered       = EXCLUDED.offered,
                            handled       = EXCLUDED.handled,
                            abandoned     = EXCLUDED.abandoned,
                            aht_seconds   = EXCLUDED.aht_seconds,
                            asa_seconds   = EXCLUDED.asa_seconds,
                            service_level = EXCLUDED.service_level
                        """
                    ),
                    {
                        "queue": queue, "ts": ts, "offered": offered,
                        "handled": handled, "abandoned": abandoned,
                        "aht": round(aht, 2), "asa": round(asa, 2),
                        "sl": round(sl, 4), "skill_id": skill_id,
                    },
                )
                n += 1
            per_run[rid] = n
            total += n

        print(f"sim_today={sim_today}  backfilled {total} actual rows")
        for rid, n in sorted(per_run.items()):
            print(f"  run {rid}: {n} intervals")
        return {"total": total, **{str(k): v for k, v in per_run.items()}}


if __name__ == "__main__":
    backfill()
