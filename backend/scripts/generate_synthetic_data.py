"""
Generate realistic synthetic interval history for development.

Why this exists: every WFM project dies waiting for "real" data from the ACD team.
This gets you to working forecasts in 5 minutes.

Realism we model:
- Daily seasonality: bell curve peaking around lunch.
- Weekly seasonality: Mon/Tue heavy, Sat/Sun light.
- Annual seasonality: gentle sinusoid (e.g. higher in Q4).
- Per-queue baseline differences (sales vs support vs retention).
- Poisson noise on volumes.
- Lognormal AHT centred per queue.
- Special-day spikes on the 1st of each month (bill-pay style).

Usage:
    # Write a CSV (then ingest via the API):
    python -m scripts.generate_synthetic_data \\
        --months 12 \\
        --queues sales,support,retention \\
        --out /tmp/intervals.csv

    # Or seed the DB directly:
    python -m scripts.generate_synthetic_data --seed-db
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

# Per-queue archetypes. Tweak freely.
QUEUE_PROFILES: dict[str, dict] = {
    "sales": {
        "channel": "voice",
        "daily_peak_calls": 90,    # peak interval volume
        "aht_mean_sec": 360,       # 6 min average handle time
        "aht_sigma": 0.35,         # lognormal sigma
    },
    "support": {
        "channel": "voice",
        "daily_peak_calls": 140,
        "aht_mean_sec": 540,       # 9 min — support calls run longer
        "aht_sigma": 0.45,
    },
    "retention": {
        "channel": "voice",
        "daily_peak_calls": 50,
        "aht_mean_sec": 720,       # 12 min — retention calls are LONG
        "aht_sigma": 0.40,
    },
}


# Phase 8 — per-skill profiles. Each `share_*` is a fraction of the queue's
# total volume to attribute to that skill at a given moment. Distinct
# seasonality per skill (sales-Monday peak, support-lunch dip, billing
# month-end spike) gives the multi-skill forecaster real signal to learn.
SKILL_PROFILES: dict[str, dict] = {
    "sales": {
        "share_baseline": 0.30,
        "weekly_mult": [1.25, 1.10, 1.00, 0.95, 0.85, 0.65, 0.50],
        "intraday_bump_hour": 16.5,
        "intraday_bump_height": 0.35,
        "intraday_dip_hour": None,
        "intraday_dip_depth": 0.0,
        "month_end_lift": 1.0,
        "aht_mean_sec": 360,
        "aht_sigma": 0.35,
    },
    "support": {
        "share_baseline": 0.55,
        "weekly_mult": [1.00, 1.00, 1.00, 1.00, 1.00, 0.85, 0.80],
        "intraday_bump_hour": None,
        "intraday_bump_height": 0.0,
        "intraday_dip_hour": 12.5,
        "intraday_dip_depth": 0.20,
        "month_end_lift": 1.0,
        "aht_mean_sec": 540,
        "aht_sigma": 0.45,
    },
    "billing": {
        "share_baseline": 0.15,
        "weekly_mult": [1.05, 1.00, 1.00, 1.00, 1.05, 0.60, 0.50],
        "intraday_bump_hour": 17.5,
        "intraday_bump_height": 0.40,
        "intraday_dip_hour": None,
        "intraday_dip_depth": 0.0,
        "month_end_lift": 1.80,
        "aht_mean_sec": 420,
        "aht_sigma": 0.40,
    },
}


def skill_share(skill: str, when: datetime) -> float:
    """Per-skill volume multiplier at a moment in time.

    Independent shares (NOT a normalized softmax) — a busy day for one skill
    doesn't drain volume from the others. Caller multiplies the queue's
    expected volume by each skill's share to get per-skill volume.
    """
    p = SKILL_PROFILES.get(skill)
    if p is None:
        return 0.0
    s = p["share_baseline"]
    s *= p["weekly_mult"][when.weekday()]

    hour = when.hour + when.minute / 60.0
    if p["intraday_bump_hour"] is not None:
        bump = math.exp(-((hour - p["intraday_bump_hour"]) ** 2) / 2.0)
        s *= 1.0 + p["intraday_bump_height"] * bump
    if p["intraday_dip_hour"] is not None:
        dip = math.exp(-((hour - p["intraday_dip_hour"]) ** 2) / 1.5)
        s *= 1.0 - p["intraday_dip_depth"] * dip

    # Month-end effect — billing spikes on the 1st and the last 2 days.
    from calendar import monthrange

    last_day = monthrange(when.year, when.month)[1]
    if when.day in (last_day, last_day - 1, 1):
        s *= p["month_end_lift"]
    return max(0.0, s)


def _daily_curve(hour: float) -> float:
    """Returns a 0..1 multiplier shaped like a contact-center day.

    Closed midnight–7am, ramps up, peaks at 11–13, dips slightly, second
    smaller peak around 16–17, tails off after 20.
    """
    if hour < 7 or hour >= 21:
        return 0.0
    # Two Gaussian bumps + small baseline.
    bump1 = math.exp(-((hour - 12) ** 2) / (2 * 2.0**2))   # lunch peak
    bump2 = 0.6 * math.exp(-((hour - 16.5) ** 2) / (2 * 1.5**2))  # afternoon
    return min(1.0, 0.15 + bump1 + bump2)


def _weekly_multiplier(weekday: int) -> float:
    """0=Mon ... 6=Sun. Mon/Tue heavy, weekend light."""
    return [1.10, 1.05, 1.00, 0.97, 0.90, 0.55, 0.45][weekday]


def _annual_multiplier(day_of_year: int) -> float:
    """Gentle sinusoid with a Q4 lift."""
    base = 1.0 + 0.08 * math.sin(2 * math.pi * (day_of_year - 60) / 365)
    q4_lift = 1.15 if 305 <= day_of_year <= 365 else 1.0
    return base * q4_lift


def _special_day_multiplier(date: datetime) -> float:
    """Bill-pay-style spike on the 1st of each month."""
    return 1.40 if date.day == 1 else 1.0


def generate(
    queues: list[str],
    start: datetime,
    end: datetime,
    interval_minutes: int = 30,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows: list[dict] = []

    cursor = start
    step = timedelta(minutes=interval_minutes)

    while cursor < end:
        hour_float = cursor.hour + cursor.minute / 60.0
        daily = _daily_curve(hour_float)
        if daily == 0.0:
            cursor += step
            continue

        weekly = _weekly_multiplier(cursor.weekday())
        annual = _annual_multiplier(cursor.timetuple().tm_yday)
        special = _special_day_multiplier(cursor)
        multiplier = daily * weekly * annual * special

        for q in queues:
            profile = QUEUE_PROFILES.get(q, QUEUE_PROFILES["support"])
            expected = profile["daily_peak_calls"] * multiplier
            offered = int(rng.poisson(expected))

            # AHT: lognormal around the queue mean.
            mu = math.log(profile["aht_mean_sec"]) - (profile["aht_sigma"] ** 2) / 2
            aht = float(rng.lognormal(mu, profile["aht_sigma"]))

            # Abandons: a small fraction; rises if offered is high vs typical.
            abandon_rate = min(0.15, 0.02 + max(0, (offered - expected) / max(1, expected)) * 0.05)
            abandoned = int(rng.binomial(offered, abandon_rate)) if offered > 0 else 0
            handled = offered - abandoned

            asa = max(0.0, float(rng.normal(20 + abandon_rate * 200, 8)))
            sl = max(0.0, min(1.0, 1.0 - (asa / 80.0)))

            rows.append({
                "queue": q,
                "channel": profile["channel"],
                "interval_start": cursor.replace(tzinfo=timezone.utc).isoformat(),
                "interval_minutes": interval_minutes,
                "offered": offered,
                "handled": handled,
                "abandoned": abandoned,
                "aht_seconds": round(aht, 2),
                "asa_seconds": round(asa, 2),
                "service_level": round(sl, 4),
            })

        cursor += step

    return pd.DataFrame(rows)


def generate_per_skill(
    queue: str,
    skills: list[str],
    start: datetime,
    end: datetime,
    interval_minutes: int = 30,
    seed: int = 42,
) -> pd.DataFrame:
    """Phase 8 generator — one row per (queue, skill, interval) instead of
    per (queue, interval). Same daily/weekly/annual seasonality as `generate`,
    plus per-skill share applied at each interval.

    Output columns include `skill` (string) and `skill_id` (None — looked up
    by name at seed time). The aggregate `interval_history` row for the
    queue (skill_id = NULL) is NOT produced here; callers that want it can
    sum the per-skill rows or run `generate()` separately.
    """
    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    profile = QUEUE_PROFILES.get(queue, QUEUE_PROFILES["support"])
    cursor = start
    step = timedelta(minutes=interval_minutes)

    while cursor < end:
        hour_float = cursor.hour + cursor.minute / 60.0
        daily = _daily_curve(hour_float)
        if daily == 0.0:
            cursor += step
            continue
        weekly = _weekly_multiplier(cursor.weekday())
        annual = _annual_multiplier(cursor.timetuple().tm_yday)
        special = _special_day_multiplier(cursor)
        queue_multiplier = daily * weekly * annual * special
        queue_expected = profile["daily_peak_calls"] * queue_multiplier

        for skill in skills:
            sp = SKILL_PROFILES.get(skill)
            if sp is None:
                continue
            expected = queue_expected * skill_share(skill, cursor)
            offered = int(rng.poisson(max(0.0, expected)))

            mu = math.log(sp["aht_mean_sec"]) - (sp["aht_sigma"] ** 2) / 2
            aht = float(rng.lognormal(mu, sp["aht_sigma"]))

            abandon_rate = min(
                0.15,
                0.02 + max(0, (offered - expected) / max(1.0, expected)) * 0.05,
            )
            abandoned = int(rng.binomial(offered, abandon_rate)) if offered > 0 else 0
            handled = offered - abandoned
            asa = max(0.0, float(rng.normal(20 + abandon_rate * 200, 8)))
            sl = max(0.0, min(1.0, 1.0 - (asa / 80.0)))

            rows.append(
                {
                    "queue": queue,
                    "channel": profile["channel"],
                    "skill": skill,
                    "interval_start": cursor.replace(tzinfo=timezone.utc).isoformat(),
                    "interval_minutes": interval_minutes,
                    "offered": offered,
                    "handled": handled,
                    "abandoned": abandoned,
                    "aht_seconds": round(aht, 2),
                    "asa_seconds": round(asa, 2),
                    "service_level": round(sl, 4),
                }
            )
        cursor += step

    return pd.DataFrame(rows)


def _seed_db(df: pd.DataFrame) -> int:
    """Write directly to Postgres. Imported lazily so the CSV-only path
    doesn't pull in DB deps."""
    from app.db import SessionLocal

    insert_sql = text("""
        INSERT INTO interval_history (
            queue, channel, interval_start, interval_minutes,
            offered, handled, abandoned, aht_seconds, asa_seconds, service_level
        ) VALUES (
            :queue, :channel, :interval_start, :interval_minutes,
            :offered, :handled, :abandoned, :aht_seconds, :asa_seconds, :service_level
        )
        ON CONFLICT (queue, channel, interval_start) DO UPDATE SET
            offered = EXCLUDED.offered,
            handled = EXCLUDED.handled,
            abandoned = EXCLUDED.abandoned,
            aht_seconds = EXCLUDED.aht_seconds,
            asa_seconds = EXCLUDED.asa_seconds,
            service_level = EXCLUDED.service_level
    """)

    rows = df.to_dict(orient="records")
    with SessionLocal() as db:
        # Chunk so we don't blow memory on big runs.
        chunk = 5000
        for i in range(0, len(rows), chunk):
            db.execute(insert_sql, rows[i : i + chunk])
        db.commit()
    return len(rows)


def _seed_db_per_skill(df: pd.DataFrame) -> int:
    """Phase 8 — write per-(queue, skill, interval) rows.

    Resolves skill names to skill_ids inside the same session. UNIQUE
    constraint on interval_history is (queue, channel, interval_start),
    which doesn't include skill_id, so we can't have both per-skill rows
    AND aggregate rows in the same DB without dropping that constraint.
    For Phase 8 stage 1 we accept per-skill-only mode (the aggregate roll-up
    can be computed at query time with SUM by interval).
    """
    from app.db import SessionLocal

    with SessionLocal() as db:
        skill_id_by_name: dict[str, int] = {}
        for name in df["skill"].unique():
            row = db.execute(
                text("SELECT id FROM skills WHERE name = :n"),
                {"n": str(name)},
            ).scalar_one_or_none()
            if row is None:
                row = db.execute(
                    text(
                        """
                        INSERT INTO skills (name, description)
                        VALUES (:n, :d)
                        RETURNING id
                        """
                    ),
                    {"n": str(name), "d": f"Auto-seeded for synthetic data: {name}"},
                ).scalar_one()
            skill_id_by_name[str(name)] = int(row)

        rows = df.to_dict(orient="records")
        for r in rows:
            r["skill_id"] = skill_id_by_name[str(r["skill"])]

        # The unique constraint is (queue, channel, interval_start), so we
        # can't have multiple per-skill rows. The Phase 8 schema migration
        # will need to update this constraint to include skill_id before
        # this seed mode is usable end-to-end. For now, fail loud if an
        # operator tries to mix modes.
        # TODO Phase 8 stage 2: add (queue, channel, interval_start, skill_id)
        # unique constraint with NULLS NOT DISTINCT, then re-enable upsert.
        # Until then, this seeder requires an empty interval_history table.
        existing = db.execute(text("SELECT COUNT(*) FROM interval_history")).scalar_one()
        if int(existing) > 0:
            raise RuntimeError(
                "interval_history is not empty — per-skill seeding requires the "
                "Phase 8 stage 2 unique-constraint update before it can coexist "
                "with aggregate rows. Truncate the table first or seed per-skill "
                "into a fresh DB."
            )

        chunk = 5000
        for i in range(0, len(rows), chunk):
            db.execute(
                text(
                    """
                    INSERT INTO interval_history (
                        queue, channel, skill_id, interval_start, interval_minutes,
                        offered, handled, abandoned, aht_seconds, asa_seconds, service_level
                    ) VALUES (
                        :queue, :channel, :skill_id, :interval_start, :interval_minutes,
                        :offered, :handled, :abandoned, :aht_seconds, :asa_seconds, :service_level
                    )
                    """
                ),
                rows[i : i + chunk],
            )
        db.commit()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate synthetic interval history.")
    p.add_argument("--months", type=int, default=12, help="How many months of history (default 12).")
    p.add_argument(
        "--queues",
        type=str,
        default="sales,support,retention",
        help="Comma-separated queue names. Unknown ones use the 'support' profile.",
    )
    p.add_argument(
        "--per-skill",
        action="store_true",
        help=(
            "Phase 8: emit per-(queue, skill, interval) rows using SKILL_PROFILES. "
            "Only one queue is supported in this mode (use --queues sales — the "
            "skills sales/support/billing become the row dimension). Empty DB "
            "required if combined with --seed-db."
        ),
    )
    p.add_argument(
        "--skills",
        type=str,
        default="sales,support,billing",
        help="Used with --per-skill. Default: sales,support,billing.",
    )
    p.add_argument("--interval-minutes", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", type=str, help="Write to this CSV path.")
    p.add_argument("--seed-db", action="store_true", help="Write directly to Postgres.")
    args = p.parse_args(argv)

    if not args.out and not args.seed_db:
        p.error("Provide either --out PATH or --seed-db (or both).")

    end = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30 * args.months)

    if args.per_skill:
        queues = [q.strip() for q in args.queues.split(",") if q.strip()]
        if len(queues) != 1:
            p.error("--per-skill requires exactly one queue name in --queues.")
        skills = [s.strip() for s in args.skills.split(",") if s.strip()]
        print(
            f"Generating {args.months} months for queue={queues[0]} skills={skills} (per-skill) ...",
            file=sys.stderr,
        )
        df = generate_per_skill(
            queues[0], skills, start, end, args.interval_minutes, args.seed
        )
    else:
        queues = [q.strip() for q in args.queues.split(",") if q.strip()]
        print(f"Generating {args.months} months for queues={queues} ...", file=sys.stderr)
        df = generate(queues, start, end, args.interval_minutes, args.seed)

    print(f"  {len(df):,} rows generated.", file=sys.stderr)

    if args.out:
        df.to_csv(args.out, index=False)
        print(f"  Wrote CSV to {args.out}", file=sys.stderr)

    if args.seed_db:
        if args.per_skill:
            n = _seed_db_per_skill(df)
        else:
            n = _seed_db(df)
        print(f"  Inserted/upserted {n:,} rows into interval_history.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
