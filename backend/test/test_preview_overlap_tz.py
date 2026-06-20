"""
Regression test for the naive-vs-aware datetime crash in preview_schedule_change.

The LLM supplies naive ISO datetimes (its tool schema just says "ISO
datetime"), while DB segment times round-trip through Postgres `timestamptz`
and come back tz-aware (`...+00:00`). The overlap check compared the two and
raised `TypeError: can't compare offset-naive and offset-aware datetimes`,
which surfaced as render:'error' on EVERY real preview — so the Apply button
never rendered and the whole chat write-action flow was unreachable from the
UI. Caught by the full-stack browser smoke on 2026-06-20; the backend unit +
integration tests missed it because they hand-built tz-aware datetimes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.tools.preview_schedule_change import _overlaps, _parse_iso_utc


def test_parse_iso_utc_coerces_naive_to_utc() -> None:
    naive = _parse_iso_utc("2026-06-10T17:00:00")
    assert naive.tzinfo == timezone.utc


def test_parse_iso_utc_preserves_explicit_offset() -> None:
    aware = _parse_iso_utc("2026-06-10T17:00:00+00:00")
    assert aware == datetime(2026, 6, 10, 17, 0, tzinfo=timezone.utc)


def test_overlaps_naive_proposed_vs_aware_segment_does_not_raise() -> None:
    """The exact shape that crashed: aware DB segment strings (+00:00) vs a
    naive proposed window from the LLM."""
    seg_start = "2026-06-10T16:15:00+00:00"  # tz-aware, as Postgres returns
    seg_end = "2026-06-10T16:45:00+00:00"
    proposed_start = datetime(2026, 6, 10, 16, 30)  # naive, as the LLM sends
    proposed_end = datetime(2026, 6, 10, 17, 0)
    # Must not raise, and must correctly detect the overlap.
    assert _overlaps(seg_start, seg_end, proposed_start, proposed_end) is True


def test_overlaps_disjoint_windows_return_false() -> None:
    seg_start = "2026-06-10T12:00:00+00:00"
    seg_end = "2026-06-10T13:00:00+00:00"
    proposed_start = datetime(2026, 6, 10, 17, 0)
    proposed_end = datetime(2026, 6, 10, 17, 30)
    assert _overlaps(seg_start, seg_end, proposed_start, proposed_end) is False
