"""
summarize_change — server-side rendering of a one-line summary for an apply
preview.

Decision D-3: this runs on the server, NOT via the LLM. Reasoning is
defense-in-depth: the model is one prompt-injection away from "tell the user
this is a small change when it's actually huge." The summary that goes on
the Apply button popover is the literal diff, computed deterministically.

Used in two places:
- Apply popover hover text in the frontend.
- Notification payload (`schedule_applied` / `schedule_undone` rows).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def summarize_change(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> str:
    """One-sentence summary of how the schedule moved.

    `before` and `after` are lists of `{agent_id, name, segments[]}` shape (the
    same gantt-row structure the renderer takes). We diff at the segment level
    and pick the first meaningful change to describe — most chat applies are
    a single move, and a bullet list of three sub-edits is harder to scan than
    one specific sentence.

    For a multi-edit change, the summary names the agent count + activity
    types touched, e.g. "Moved 3 agents' lunches between 12:00 and 13:00."
    """
    by_agent_before = {a["id"]: a for a in before}
    by_agent_after = {a["id"]: a for a in after}

    diffs = []
    for agent_id in by_agent_after:
        a_before = by_agent_before.get(agent_id, {"name": agent_id, "segments": []})
        a_after = by_agent_after[agent_id]
        before_segs = {(s["start"], s["end"], s["activity"]) for s in a_before.get("segments", [])}
        after_segs = {(s["start"], s["end"], s["activity"]) for s in a_after.get("segments", [])}
        added = after_segs - before_segs
        removed = before_segs - after_segs
        if added or removed:
            diffs.append((a_after["name"], list(removed), list(added)))

    if not diffs:
        return "No effective change."

    if len(diffs) == 1:
        name, removed, added = diffs[0]
        return _one_agent_phrase(name, removed, added)

    activities = {a[2] for _, _, adds in diffs for a in adds}
    if len(activities) == 1:
        activity = next(iter(activities))
        return f"Moved {len(diffs)} agents' {activity} segments."
    return f"Edited {len(diffs)} agents' schedules ({len(activities)} activity types touched)."


def _one_agent_phrase(
    name: str,
    removed: list[tuple[str, str, str]],
    added: list[tuple[str, str, str]],
) -> str:
    """Phrase the diff for a single agent.

    Common cases:
    - removed 1 + added 1 with same activity → "Moved {name}'s {activity} from {old} to {new}"
    - removed 0 + added N → "Added {N} {activity} segment(s) for {name}"
    - removed N + added 0 → "Removed {N} segment(s) from {name}"
    - everything else → fall back to a count-based phrase
    """
    if len(removed) == 1 and len(added) == 1:
        r_start, r_end, r_act = removed[0]
        a_start, a_end, a_act = added[0]
        if r_act == a_act:
            return (
                f"Moved {name}'s {a_act} from {_hhmm(r_start)} to {_hhmm(a_start)}."
            )
        return (
            f"Changed {name}'s {_hhmm(r_start)}–{_hhmm(r_end)} block "
            f"from {r_act} to {a_act}."
        )
    if not removed and added:
        kinds = {a[2] for a in added}
        kind = next(iter(kinds)) if len(kinds) == 1 else "segment"
        return f"Added {len(added)} {kind} segment(s) for {name}."
    if removed and not added:
        return f"Removed {len(removed)} segment(s) from {name}."
    return (
        f"Edited {name}'s schedule "
        f"({len(removed)} removed, {len(added)} added)."
    )


def _hhmm(iso_or_dt: str | datetime) -> str:
    if isinstance(iso_or_dt, datetime):
        return iso_or_dt.strftime("%H:%M")
    return iso_or_dt[11:16] if len(iso_or_dt) >= 16 else iso_or_dt
