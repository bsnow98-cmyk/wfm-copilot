"""
Phase 6 eval — anomaly citation + hallucination check.

Asks the model about anomalies; asserts that any monospace 16-hex anomaly id
the model emits in its response actually exists in the row set returned by
get_anomalies. Catches the obvious failure mode of "the model invented an
id and explained it confidently."

Skipped without ANTHROPIC_API_KEY and WFM_DB_TEST_URL.
"""
from __future__ import annotations

import os
import re
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("ANTHROPIC_API_KEY")
        and os.environ.get("WFM_DB_TEST_URL")
    ),
    reason="ANTHROPIC_API_KEY or WFM_DB_TEST_URL not set",
)

# Anomaly ids per Decisions.md are SHA256 truncated to 16 hex chars.
ANOMALY_ID_RE = re.compile(r"`?\b([0-9a-f]{16})\b`?")


def _run_chat(prompt: str) -> tuple[str, list[dict[str, Any]]]:
    """Replays the chat loop end-to-end. Returns (text, tool_results)."""
    from anthropic import Anthropic
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.routers.chat import SYSTEM_PROMPT
    from app.tools import all_definitions, dispatch

    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    Session = sessionmaker(bind=create_engine(os.environ["WFM_DB_TEST_URL"]))
    db = Session()

    history: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    text_out = ""
    tool_outputs: list[dict[str, Any]] = []

    for _ in range(4):
        resp = client.messages.create(
            model=settings.anthropic_model,
            system=SYSTEM_PROMPT,
            tools=all_definitions(),
            messages=history,
            max_tokens=1024,
        )
        blocks = resp.content
        history.append({"role": "assistant", "content": blocks})
        for b in blocks:
            if getattr(b, "type", None) == "text":
                text_out += b.text

        if resp.stop_reason != "tool_use":
            break

        tool_results_payload = []
        for b in blocks:
            if getattr(b, "type", None) != "tool_use":
                continue
            result = dispatch(b.name, b.input, db)
            tool_outputs.append({"name": b.name, "result": result})
            import json

            tool_results_payload.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": json.dumps(result),
                }
            )
        history.append({"role": "user", "content": tool_results_payload})

    db.close()
    return text_out, tool_outputs


def test_anomaly_ids_in_reply_exist_in_tool_result() -> None:
    text, tool_outputs = _run_chat("What anomalies happened this week?")
    anomalies_calls = [t for t in tool_outputs if t["name"] == "get_anomalies"]
    if not anomalies_calls:
        pytest.skip("Model did not call get_anomalies for this prompt.")

    valid_ids: set[str] = set()
    for call in anomalies_calls:
        result = call["result"]
        if result.get("render") != "table":
            continue
        cols = result.get("columns", [])
        if "id" not in cols:
            continue
        idx = cols.index("id")
        for row in result.get("rows", []):
            valid_ids.add(str(row[idx]))

    cited = set(ANOMALY_ID_RE.findall(text))
    bogus = cited - valid_ids
    assert not bogus, (
        f"Model cited anomaly id(s) not in the tool result: {bogus}. "
        f"Valid set: {valid_ids}. Reply: {text[:300]}..."
    )
