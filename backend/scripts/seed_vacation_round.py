"""
Seed a demo vacation bid round with HAND-TUNED contention.

The waterfall is only impressive if it visibly *does* something — so this builds
a deliberately contested round: an oversubscribed popular week (senior agents
block juniors), an agent who can't afford their pick (PTO tanked), and agents who
bid only the popular week (so they win nothing). Idempotent-ish: it deletes any
prior round named 'Demo Vacation Bid 2027' first.

    DATABASE_URL=… python -m scripts.seed_vacation_round      # or run via the API container
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import text

from app.db import SessionLocal

SEASON = [date(2027, 7, 5), date(2027, 7, 12), date(2027, 7, 19), date(2027, 7, 26)]  # Mondays
CAPACITY = {SEASON[0]: 3, SEASON[1]: 4, SEASON[2]: 5, SEASON[3]: 6}  # W1 = the prized July-4th week
NAME = "Demo Vacation Bid 2027"


def main() -> None:
    with SessionLocal() as db:
        db.execute(text("DELETE FROM bid_rounds WHERE name = :n"), {"n": NAME})  # cascades bids/capacity

        agents = db.execute(
            text(
                "SELECT id, employee_id FROM agents WHERE active = TRUE AND hire_date IS NOT NULL "
                "ORDER BY hire_date LIMIT 16"  # 16 most-senior, seniority order
            )
        ).mappings().all()
        if len(agents) < 8:
            raise SystemExit("Not enough agents with hire_date to seed a contested round.")
        ids = [int(a["id"]) for a in agents]

        rid = db.execute(
            text(
                """
                INSERT INTO bid_rounds (name, status, bids_open_at, bids_close_at,
                                        season_start, season_end, max_weeks_per_agent)
                VALUES (:n, 'closed', :open, :close, :s, :e, 2)
                RETURNING id
                """
            ),
            {
                "n": NAME,
                "open": datetime(2027, 5, 1, tzinfo=timezone.utc),
                "close": datetime(2027, 6, 1, tzinfo=timezone.utc),
                "s": SEASON[0],
                "e": SEASON[-1],
            },
        ).scalar_one()

        for w, slots in CAPACITY.items():
            db.execute(
                text("INSERT INTO bid_week_capacity (round_id, week_start, slots) VALUES (:r,:w,:s)"),
                {"r": rid, "w": w, "s": slots},
            )

        # Everyone bids W1 first (oversubscribed: 16 want it, 3 slots). Then a
        # spread of alternates — except the last two, who bid ONLY W1 (zero-win
        # once bumped).
        for i, aid in enumerate(ids):
            db.execute(
                text("INSERT INTO vacation_bids (round_id, agent_id, week_start, rank) VALUES (:r,:a,:w,1)"),
                {"r": rid, "a": aid, "w": SEASON[0]},
            )
            if i < len(ids) - 2:
                alt1 = SEASON[1 + (i % 3)]
                alt2 = SEASON[1 + ((i + 1) % 3)]
                db.execute(
                    text("INSERT INTO vacation_bids (round_id, agent_id, week_start, rank) VALUES (:r,:a,:w,2)"),
                    {"r": rid, "a": aid, "w": alt1},
                )
                if alt2 != alt1:
                    db.execute(
                        text("INSERT INTO vacation_bids (round_id, agent_id, week_start, rank) VALUES (:r,:a,:w,3)"),
                        {"r": rid, "a": aid, "w": alt2},
                    )

        # Tank one mid-seniority agent's PTO so they're denied insufficient_pto.
        broke = ids[6]
        db.execute(
            text(
                "INSERT INTO pto_ledger (agent_id, event_ts, event_type, hours, balance_after, note) "
                "VALUES (:a, sim_now(), 'adjust', -9999, 8, 'demo: low balance for bid contention')"
            ),
            {"a": broke},
        )

        db.commit()
        print(f"Seeded round {rid} '{NAME}': {len(ids)} bidders, "
              f"W1 capacity 3 (oversubscribed {len(ids)}→3), 1 can't-afford, 2 zero-win candidates.")


if __name__ == "__main__":
    main()
