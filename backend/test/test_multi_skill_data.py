"""
Phase 8 Stage 1 — synthetic data + skill-mix smoke tests.

These run without a database. They exercise the math that drives the
multi-skill data generator:

- The distribution constants in seed_agents add up to the design's 50-agent
  mix (25 single / 20 dual / 5 tri).
- skill_share respects the design's seasonality signals — sales is bigger
  Mondays than Fridays, support has a lunch dip, billing spikes on the 1st.
- generate_per_skill produces well-shaped rows with `skill` + `offered`.

Coupled with the migration (which we don't run here — that needs Postgres),
these tests are the load-bearing safety net for stage 1.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.generate_synthetic_data import (
    SKILL_PROFILES,
    generate_per_skill,
    skill_share,
)
from scripts.seed_agents import DEFAULT_SKILLS, MULTI_SKILL_DISTRIBUTION


# --------------------------------------------------------------------------
# seed_agents distribution
# --------------------------------------------------------------------------
def test_multi_skill_distribution_totals_50_agents() -> None:
    total = sum(count for count, _ in MULTI_SKILL_DISTRIBUTION)
    assert total == 50


def test_multi_skill_distribution_has_realistic_mix_by_count() -> None:
    """25 single-skill, 20 dual-skill, 5 tri-skill per design."""
    by_card = {1: 0, 2: 0, 3: 0}
    for count, assignments in MULTI_SKILL_DISTRIBUTION:
        by_card[len(assignments)] = by_card.get(len(assignments), 0) + count
    assert by_card[1] == 25, "single-skill agents"
    assert by_card[2] == 20, "dual-skill agents"
    assert by_card[3] == 5, "tri-skill (universal) agents"


def test_every_assignment_uses_only_default_skills() -> None:
    skills_used = {a.skill for _, asg in MULTI_SKILL_DISTRIBUTION for a in asg}
    assert skills_used <= set(DEFAULT_SKILLS), (
        f"Distribution references unknown skill: {skills_used - set(DEFAULT_SKILLS)}"
    )


def test_proficiency_within_design_bounds() -> None:
    """Primary 4-5, secondary 2-3, tertiary 1-2 per the design doc.

    We can't tell primary vs secondary from the position alone (the design
    keeps it implicit), so the looser assertion is: every proficiency is
    in the WFM scale 1-5.
    """
    for _, assignments in MULTI_SKILL_DISTRIBUTION:
        for a in assignments:
            assert 1 <= a.proficiency <= 5, f"proficiency out of band: {a}"


# --------------------------------------------------------------------------
# skill_share seasonality
# --------------------------------------------------------------------------
def test_sales_share_higher_monday_than_friday() -> None:
    monday_noon = datetime(2026, 4, 27, 12, 0)        # Monday
    friday_noon = datetime(2026, 5, 1, 12, 0)         # Friday
    assert friday_noon.weekday() == 4
    assert skill_share("sales", monday_noon) > skill_share("sales", friday_noon)


def test_sales_share_higher_late_afternoon_than_morning() -> None:
    """The intraday bump is at hour 16.5 — sales spikes late afternoon."""
    morning = datetime(2026, 4, 28, 9, 0)             # Tuesday
    afternoon = datetime(2026, 4, 28, 17, 0)          # Tuesday
    assert skill_share("sales", afternoon) > skill_share("sales", morning)


def test_support_dips_at_lunch() -> None:
    """Lunch dip at hour 12.5 — support volume drops mid-day."""
    eleven = datetime(2026, 4, 28, 11, 0)
    noon = datetime(2026, 4, 28, 12, 30)
    one = datetime(2026, 4, 28, 13, 30)
    assert skill_share("support", noon) < skill_share("support", eleven)
    assert skill_share("support", noon) < skill_share("support", one)


def test_billing_spikes_at_month_end_and_first() -> None:
    """The month_end_lift triggers on day 1 and the last 2 days."""
    middle = datetime(2026, 4, 15, 14, 0)
    first = datetime(2026, 5, 1, 14, 0)
    last = datetime(2026, 4, 30, 14, 0)
    second_to_last = datetime(2026, 4, 29, 14, 0)

    assert skill_share("billing", first) > skill_share("billing", middle)
    assert skill_share("billing", last) > skill_share("billing", middle)
    assert skill_share("billing", second_to_last) > skill_share("billing", middle)


def test_unknown_skill_returns_zero_share() -> None:
    assert skill_share("foo_unknown_skill", datetime(2026, 4, 28, 12, 0)) == 0.0


# --------------------------------------------------------------------------
# generate_per_skill output shape
# --------------------------------------------------------------------------
@pytest.fixture
def short_per_skill_df():
    """3 days of synthetic per-skill data for the 'sales' queue."""
    start = datetime(2026, 4, 27, tzinfo=timezone.utc)   # Monday
    end = start + timedelta(days=3)
    return generate_per_skill(
        queue="sales",
        skills=list(DEFAULT_SKILLS),
        start=start,
        end=end,
        interval_minutes=30,
        seed=7,
    )


def test_per_skill_df_has_expected_columns(short_per_skill_df) -> None:
    expected = {
        "queue", "channel", "skill", "interval_start", "interval_minutes",
        "offered", "handled", "abandoned", "aht_seconds", "asa_seconds",
        "service_level",
    }
    assert expected <= set(short_per_skill_df.columns)


def test_per_skill_df_covers_all_three_skills(short_per_skill_df) -> None:
    assert set(short_per_skill_df["skill"].unique()) == set(DEFAULT_SKILLS)


def test_per_skill_volume_ordering_matches_share_baseline(short_per_skill_df) -> None:
    """Sum across the 3 days: support > sales > billing, matching the
    share_baseline ordering (0.55 > 0.30 > 0.15)."""
    totals = short_per_skill_df.groupby("skill")["offered"].sum().to_dict()
    assert totals["support"] > totals["sales"] > totals["billing"], (
        f"Skill volume ordering off — got totals={totals}. "
        "Expected support > sales > billing."
    )


def test_skill_profiles_share_baselines_sum_close_to_one() -> None:
    """The three baselines are a sanity check, not a hard constraint —
    skill_share() is intentionally non-normalized. But if the baselines
    drift far from 1.0, the per-skill series total will diverge wildly
    from the queue-level series, which usually means a typo."""
    s = sum(p["share_baseline"] for p in SKILL_PROFILES.values())
    assert 0.85 <= s <= 1.15, f"share_baseline sum is {s}; check SKILL_PROFILES"
