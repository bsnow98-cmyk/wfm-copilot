"""
Seed synthetic agents for the scheduling solver.

Phase 8 update — supports multi-skill agent rosters per the design doc:

  python -m scripts.seed_agents --multi-skill

That distributes 50 agents across `sales`, `support`, `billing`:
  - 25 single-skill (12 support + 8 sales + 5 billing — support is biggest).
  - 20 dual-skill (10 sales+support, 8 support+billing, 2 sales+billing).
  - 5 tri-skill ("universal agents" — older, experienced).

Idempotent: if employee_id already exists, the row is left alone but its
skill assignments are upserted to match the v8 distribution. To go back to
the Phase 1 behavior (all on a single skill), pass `--single-skill sales`.

Usage:
    docker compose exec api python -m scripts.seed_agents --multi-skill
    docker compose exec api python -m scripts.seed_agents --single-skill sales --count 50
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import text


# Phase 8 v1 skill set. Stage 2 reads from this (and from the database) to
# compute per-skill demand. Order is stable so seeding is deterministic.
DEFAULT_SKILLS = ["sales", "support", "billing"]


@dataclass(frozen=True)
class SkillProf:
    skill: str
    proficiency: int  # 1 (learner) .. 5 (expert)


# Distribution matches the design doc's call-center-realistic mix.
# Each entry is (count, [(skill, proficiency), ...]).
MULTI_SKILL_DISTRIBUTION: list[tuple[int, list[SkillProf]]] = [
    # Single-skill (25 total) — primary skill at proficiency 4-5.
    (12, [SkillProf("support", 4)]),
    (8, [SkillProf("sales", 5)]),
    (5, [SkillProf("billing", 4)]),
    # Dual-skill (20 total) — primary at 4, secondary at 2-3.
    (10, [SkillProf("sales", 4), SkillProf("support", 3)]),
    (8, [SkillProf("support", 4), SkillProf("billing", 2)]),
    (2, [SkillProf("sales", 4), SkillProf("billing", 2)]),
    # Tri-skill (5 total) — universal agents, primary at 5, others 3.
    (5, [SkillProf("support", 5), SkillProf("sales", 3), SkillProf("billing", 3)]),
]


def _ensure_skill(db, name: str) -> int:
    db.execute(
        text(
            """
            INSERT INTO skills (name, description)
            VALUES (:name, :desc)
            ON CONFLICT (name) DO NOTHING
            """
        ),
        {"name": name, "desc": f"Auto-seeded skill: {name}"},
    )
    return db.execute(
        text("SELECT id FROM skills WHERE name = :name"),
        {"name": name},
    ).scalar_one()


def _ensure_agent(db, *, employee_id: str, full_name: str) -> int:
    """Returns agent.id, creating the row if needed."""
    res = db.execute(
        text(
            """
            INSERT INTO agents
                (employee_id, full_name, email, contracted_hours_per_week,
                 timezone, active)
            VALUES (:emp_id, :name, :email, 40, 'America/New_York', TRUE)
            ON CONFLICT (employee_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "emp_id": employee_id,
            "name": full_name,
            "email": f"{employee_id.lower()}@example.com",
        },
    )
    row = res.fetchone()
    if row is not None:
        return int(row[0])
    return int(
        db.execute(
            text("SELECT id FROM agents WHERE employee_id = :emp"),
            {"emp": employee_id},
        ).scalar_one()
    )


def _assign_skills(db, agent_id: int, assignments: list[SkillProf], skill_ids: dict[str, int]) -> None:
    """Upsert agent_skills rows for this agent. Existing rows for skills not in
    the new list are deleted — so reseeding moves an agent from old mix to new
    mix without leaving orphan assignments."""
    db.execute(
        text(
            """
            DELETE FROM agent_skills
            WHERE agent_id = :aid AND skill_id <> ALL(:keep_ids)
            """
        ),
        {
            "aid": agent_id,
            "keep_ids": [skill_ids[a.skill] for a in assignments],
        },
    )
    for a in assignments:
        db.execute(
            text(
                """
                INSERT INTO agent_skills (agent_id, skill_id, proficiency)
                VALUES (:aid, :sid, :prof)
                ON CONFLICT (agent_id, skill_id)
                  DO UPDATE SET proficiency = EXCLUDED.proficiency
                """
            ),
            {"aid": agent_id, "sid": skill_ids[a.skill], "prof": a.proficiency},
        )


def seed_multi_skill() -> None:
    """50 agents per the Phase 8 distribution."""
    from app.db import SessionLocal

    with SessionLocal() as db:
        skill_ids = {name: _ensure_skill(db, name) for name in DEFAULT_SKILLS}

        agent_index = 0
        new_agents = 0
        for count, assignments in MULTI_SKILL_DISTRIBUTION:
            for _ in range(count):
                agent_index += 1
                emp_id = f"EMP{agent_index:03d}"
                exists_before = db.execute(
                    text("SELECT 1 FROM agents WHERE employee_id = :e"),
                    {"e": emp_id},
                ).scalar_one_or_none()
                agent_id = _ensure_agent(
                    db, employee_id=emp_id, full_name=f"Agent {agent_index:03d}"
                )
                _assign_skills(db, agent_id, assignments, skill_ids)
                if not exists_before:
                    new_agents += 1

        db.commit()

        print(
            f"Phase 8 multi-skill seed complete. {new_agents} new agents, "
            f"{agent_index} total. Skills: {DEFAULT_SKILLS}.",
            file=sys.stderr,
        )


def seed_single_skill(count: int = 50, skill_name: str = "sales") -> None:
    """Phase 1 compat: every agent gets one skill at proficiency 3."""
    from app.db import SessionLocal

    with SessionLocal() as db:
        skill_ids = {skill_name: _ensure_skill(db, skill_name)}

        new_count = 0
        for i in range(1, count + 1):
            emp_id = f"EMP{i:03d}"
            exists_before = db.execute(
                text("SELECT 1 FROM agents WHERE employee_id = :e"),
                {"e": emp_id},
            ).scalar_one_or_none()
            agent_id = _ensure_agent(db, employee_id=emp_id, full_name=f"Agent {i:03d}")
            _assign_skills(db, agent_id, [SkillProf(skill_name, 3)], skill_ids)
            if not exists_before:
                new_count += 1

        db.commit()
        total = db.execute(
            text("SELECT COUNT(*) FROM agents WHERE active = TRUE")
        ).scalar_one()
        print(
            f"Seeded {new_count} new agents on skill '{skill_name}'. "
            f"Total active agents now: {total}.",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Seed synthetic agents.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--multi-skill",
        action="store_true",
        help="Phase 8: 50 agents with realistic 3-skill distribution.",
    )
    mode.add_argument(
        "--single-skill",
        type=str,
        metavar="SKILL",
        help="Phase 1 compat: every agent on the named skill at proficiency 3.",
    )
    p.add_argument(
        "--count",
        type=int,
        default=50,
        help="Only used with --single-skill (default 50).",
    )
    args = p.parse_args(argv)

    # Default to multi-skill if neither flag was passed — the new sensible default.
    if args.multi_skill or (not args.multi_skill and not args.single_skill):
        seed_multi_skill()
    else:
        seed_single_skill(count=args.count, skill_name=args.single_skill)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
