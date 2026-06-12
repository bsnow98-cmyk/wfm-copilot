"""
Faithfulness eval — "the AI shows its math" as a checked property.

For each prompt: replay the chat loop end-to-end, capture every tool result
the model saw, then ask a judge model whether every NUMBER in the assistant's
prose is supported by those tool results. Catches the failure mode the
anomaly-id eval catches for ids, generalized to all quantitative claims:
"the model summarized a chart it never received."

Run alongside eval_tool_selection.py whenever prompts/tools/model change:
    WFM_DB_TEST_URL=postgresql+psycopg://... python -m pytest test/eval_faithfulness.py -v

Skipped without ANTHROPIC_API_KEY and WFM_DB_TEST_URL.
"""
from __future__ import annotations

import json
import os
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("ANTHROPIC_API_KEY")
        and os.environ.get("WFM_DB_TEST_URL")
    ),
    reason="ANTHROPIC_API_KEY or WFM_DB_TEST_URL not set",
)

PROMPTS = [
    "What does today's forecast look like?",
    "How is adherence trending over the last two weeks?",
    "Any anomalies in the last week I should know about?",
    "Why did we miss service level recently, and what should we do about it?",
    "Who are the top agents by tenure?",
]

JUDGE_SYSTEM = """You are auditing a workforce-management assistant for numeric \
faithfulness. You get the assistant's reply and the raw JSON tool results it \
received. Decide whether every specific number in the reply (counts, percents, \
times, scores, token-free quantities) is directly supported by the tool results \
or by trivial arithmetic on them (a sum, a rounding, a difference). General \
words without numbers are fine. A reply with no numbers is trivially faithful.

Respond with ONLY a JSON object: {"faithful": true/false, "violations": \
["<number> — <why unsupported>", ...]}"""


def _run_chat(prompt: str) -> tuple[str, list[dict[str, Any]]]:
    """Replay the chat loop. Returns (assistant_text, tool_results)."""
    from anthropic import Anthropic
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.routers.chat import SYSTEM_PROMPT, _strict_tool
    from app.tools import all_definitions, dispatch

    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    Session = sessionmaker(bind=create_engine(os.environ["WFM_DB_TEST_URL"]))
    db = Session()
    tools = [_strict_tool(d) for d in all_definitions()]

    history: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    text_out = ""
    tool_outputs: list[dict[str, Any]] = []
    try:
        for _ in range(6):
            resp = client.messages.create(
                model=settings.anthropic_model,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=history,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                max_tokens=4096,
            )
            history.append({"role": "assistant", "content": resp.content})
            text_out = "".join(
                b.text for b in resp.content if b.type == "text"
            ) or text_out
            if resp.stop_reason != "tool_use":
                break
            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                out = dispatch(b.name, b.input, db)
                db.rollback()
                tool_outputs.append({"tool": b.name, "result": out})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": json.dumps(out, default=str),
                })
            history.append({"role": "user", "content": results})
    finally:
        db.close()
    return text_out, tool_outputs


def _judge(reply: str, tool_outputs: list[dict[str, Any]]) -> dict[str, Any]:
    from anthropic import Anthropic

    from app.config import get_settings

    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        system=JUDGE_SYSTEM,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"ASSISTANT REPLY:\n{reply}\n\n"
                f"TOOL RESULTS:\n{json.dumps(tool_outputs, default=str)[:30000]}"
            ),
        }],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Tolerate code fences around the JSON.
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"faithful": False, "violations": [f"judge output unparseable: {raw[:200]}"]}


@pytest.mark.parametrize("prompt", PROMPTS)
def test_reply_numbers_are_grounded_in_tool_results(prompt: str) -> None:
    reply, tool_outputs = _run_chat(prompt)
    assert reply.strip(), f"No assistant text for prompt: {prompt!r}"
    verdict = _judge(reply, tool_outputs)
    assert verdict.get("faithful") is True, (
        f"Unfaithful reply for {prompt!r}.\n"
        f"Violations: {verdict.get('violations')}\n"
        f"Reply was: {reply[:500]}"
    )
