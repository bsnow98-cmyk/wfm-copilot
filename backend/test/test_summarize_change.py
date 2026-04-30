"""
Unit tests for the summarize_change helper.

The phrasing matters — the popover and notification both surface this string,
and "the AI shows its math" requires it to be specific and accurate. We test
the four common shapes the helper handles, plus the no-op case.
"""
from __future__ import annotations

from app.services.summarize_change import summarize_change


def _agent(name: str, segments: list[tuple[str, str, str]]) -> dict:
    return {
        "id": name.lower().replace(" ", "_"),
        "name": name,
        "segments": [
            {"start": s, "end": e, "activity": a} for s, e, a in segments
        ],
    }


def test_single_agent_lunch_window_move() -> None:
    """Realistic case: moving lunch shifts the bracketing 'available' segments
    too, so the diff sees 3 removed + 3 added. The summary should name the
    agent and admit it's a multi-segment edit; we don't yet detect 'lunch
    boundary shift' as a higher-level pattern."""
    before = [
        _agent(
            "Adams, J.",
            [
                ("2026-04-29T08:00:00", "2026-04-29T12:00:00", "available"),
                ("2026-04-29T12:00:00", "2026-04-29T12:30:00", "lunch"),
                ("2026-04-29T12:30:00", "2026-04-29T16:00:00", "available"),
            ],
        )
    ]
    after = [
        _agent(
            "Adams, J.",
            [
                ("2026-04-29T08:00:00", "2026-04-29T13:00:00", "available"),
                ("2026-04-29T13:00:00", "2026-04-29T13:30:00", "lunch"),
                ("2026-04-29T13:30:00", "2026-04-29T16:00:00", "available"),
            ],
        )
    ]
    summary = summarize_change(before, after)
    assert "Adams, J." in summary
    assert "3 removed" in summary and "3 added" in summary


def test_single_segment_swap_uses_specific_phrase() -> None:
    """When the diff is a clean 1-removed/1-added with same activity, the
    summary uses the time-move phrasing — that's the case worth optimising."""
    before = [
        _agent("Adams, J.", [("2026-04-29T12:00:00", "2026-04-29T12:30:00", "lunch")]),
    ]
    after = [
        _agent("Adams, J.", [("2026-04-29T13:00:00", "2026-04-29T13:30:00", "lunch")]),
    ]
    summary = summarize_change(before, after)
    assert "Moved" in summary
    assert "lunch" in summary
    assert "12:00" in summary
    assert "13:00" in summary


def test_multiple_agents_same_activity() -> None:
    before = [
        _agent("A", [("2026-04-29T12:00:00", "2026-04-29T12:30:00", "lunch")]),
        _agent("B", [("2026-04-29T12:00:00", "2026-04-29T12:30:00", "lunch")]),
        _agent("C", [("2026-04-29T12:00:00", "2026-04-29T12:30:00", "lunch")]),
    ]
    after = [
        _agent("A", [("2026-04-29T13:00:00", "2026-04-29T13:30:00", "lunch")]),
        _agent("B", [("2026-04-29T13:00:00", "2026-04-29T13:30:00", "lunch")]),
        _agent("C", [("2026-04-29T13:00:00", "2026-04-29T13:30:00", "lunch")]),
    ]
    summary = summarize_change(before, after)
    assert "3 agents" in summary
    assert "lunch" in summary


def test_no_effective_change() -> None:
    state = [
        _agent("A", [("2026-04-29T12:00:00", "2026-04-29T12:30:00", "lunch")])
    ]
    assert summarize_change(state, state) == "No effective change."


def test_activity_change_same_window() -> None:
    before = [
        _agent("A", [("2026-04-29T12:00:00", "2026-04-29T12:30:00", "lunch")])
    ]
    after = [
        _agent("A", [("2026-04-29T12:00:00", "2026-04-29T12:30:00", "training")])
    ]
    summary = summarize_change(before, after)
    assert "lunch" in summary
    assert "training" in summary
