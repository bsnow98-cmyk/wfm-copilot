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
    # ---- Wave 3 — adherence ----
    {
        "prompt": "How is adherence trending this week?",
        "expected_tool": "get_adherence",
    },
    {
        "prompt": "Who was late today?",
        "expected_tool": "get_exceptions",
    },
    {
        "prompt": "Why was adherence low yesterday?",
        "expected_tool": "explain_adherence_drop",
    },
    {
        "prompt": "Are people working their scheduled hours this week?",
        "expected_tool": "get_conformance",
    },
    # ---- Wave 3 — real-time ----
    {
        "prompt": "How are we doing right now?",
        "expected_tool": "get_realtime_status",
    },
    {
        "prompt": "Who's on break right now?",
        "expected_tool": "get_agents_on_aux",
    },
    {
        "prompt": "Anything firing right now? Any alerts?",
        "expected_tool": "get_realtime_alerts",
    },
    {
        "prompt": "We're short staffed — can we move some breaks earlier by 30 minutes?",
        "expected_tool": "recommend_break_shift",
    },
    # ---- Wave 3 — PTO/leave ----
    {
        "prompt": "How much PTO does EMP001 have left?",
        "expected_tool": "get_pto_balance",
    },
    {
        "prompt": "What PTO requests are pending approval?",
        "expected_tool": "get_leave_requests",
    },
    {
        "prompt": "Work my PTO approval queue — which ones should I approve?",
        "expected_tool": "recommend_leave_approval",
    },
    # ---- Wave 4 — performance ----
    {
        "prompt": "Tell me about EMP001.",
        "expected_tool": "get_agent_performance",
    },
    {
        "prompt": "Show me the top 10 agents by QA score.",
        "expected_tool": "rank_agents",
    },
    {
        "prompt": "How is the team doing this week?",
        "expected_tool": "get_team_kpis",
    },
    {
        "prompt": "Who's at risk of leaving?",
        "expected_tool": "get_attrition_risk",
    },
    {
        "prompt": "How is the new hire class doing?",
        "expected_tool": "get_new_hire_progress",
    },
    # ---- Wave 4 — training ----
    {
        "prompt": "What training is on the calendar for the next two weeks?",
        "expected_tool": "get_training_calendar",
    },
    {
        "prompt": "When can I coach EMP001 in the next week?",
        "expected_tool": "recommend_coaching_slot",
    },
    {
        "prompt": "Who is certified on billing?",
        "expected_tool": "get_skill_certifications",
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
