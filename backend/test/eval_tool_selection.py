"""
Phase 6 eval — does the LLM pick the right tool for each prompt?

Skipped if ANTHROPIC_API_KEY is unset. Run with:
    pytest -xvs backend/test/eval_tool_selection.py

The cases are deliberately small and unambiguous. If you tweak the system
prompt or add a tool, adjust here too.
"""
from __future__ import annotations

import os
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)

CASES: list[dict[str, Any]] = [
    {
        "prompt": "Show today's forecast for sales_inbound",
        "expected_tool": "get_forecast",
    },
    {
        "prompt": "How many agents do we need at 80/20 for sales_inbound?",
        "expected_tool": "get_staffing",
    },
    {
        "prompt": "What does today's schedule look like?",
        "expected_tool": "get_schedule",
    },
    {
        "prompt": "Anything weird in the data this week?",
        "expected_tool": "get_anomalies",
    },
    {
        "prompt": "Compare 80/20 vs 90/15 for sales_inbound",
        "expected_tool": "compare_scenarios",
    },
    {
        "prompt": "What if Adams takes lunch at 13:00 instead of 12:30 today?",
        "expected_tool": "preview_schedule_change",
    },
    # Phase 8 stage 5 — new tools.
    {
        # `get_skills_coverage` requires a queue. The previous prompt
        # ("How is each skill covered today?") was underspecified — the
        # model correctly asked for clarification instead of guessing,
        # which is the desired UX. Real users include the queue. Verified
        # 2026-05-01 against `claude-sonnet-4-5-20250929`: this prompt
        # routes to `get_skills_coverage` with queue='sales_inbound'.
        "prompt": "How is each skill covered for sales_inbound today?",
        "expected_tool": "get_skills_coverage",
    },
    {
        "prompt": "Why does sales need that many agents? Show me the substitution math.",
        "expected_tool": "explain_substitution",
    },
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["expected_tool"])
def test_llm_picks_correct_tool(case: dict[str, Any]) -> None:
    from anthropic import Anthropic

    from app.config import get_settings
    from app.routers.chat import SYSTEM_PROMPT
    from app.tools import all_definitions

    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        system=SYSTEM_PROMPT,
        tools=all_definitions(),
        messages=[{"role": "user", "content": case["prompt"]}],
        max_tokens=512,
    )
    tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
    assert tool_uses, f"Model did not call a tool for: {case['prompt']!r}"
    assert tool_uses[0].name == case["expected_tool"], (
        f"Expected {case['expected_tool']!r}, got {tool_uses[0].name!r} "
        f"for prompt {case['prompt']!r}"
    )
