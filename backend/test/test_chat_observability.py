"""
Unit tests for the chat-observability helpers. The DB is mocked — the
SQL contains Postgres-specific JSONB/UUID casts so a sqlite shim wouldn't
add value here. The integration assertion (the migration applies, the
columns exist) is covered in CI when Postgres is up.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.services.chat_observability import (
    ConversationFunnel,
    ToolCallLog,
    conversation_funnel,
    log_tool_call,
)


def test_log_tool_call_inserts_with_expected_params() -> None:
    db = MagicMock()
    entry = ToolCallLog(
        conversation_id="11111111-1111-1111-1111-111111111111",
        user_msg_id="22222222-2222-2222-2222-222222222222",
        tool_name="get_forecast",
        args={"queue": "sales_inbound"},
        latency_ms=42,
        error=None,
        tokens_in=120,
        tokens_out=60,
    )
    log_tool_call(db, entry)

    assert db.execute.call_count == 1
    _, kwargs = db.execute.call_args
    params = db.execute.call_args.args[1] if len(db.execute.call_args.args) > 1 else kwargs
    # call_args.args is (text_clause, params_dict)
    params = db.execute.call_args.args[1]
    assert params["cid"] == entry.conversation_id
    assert params["umid"] == entry.user_msg_id
    assert params["tool"] == "get_forecast"
    assert json.loads(params["args"]) == {"queue": "sales_inbound"}
    assert params["latency"] == 42
    assert params["error"] is None
    assert params["tin"] == 120
    assert params["tout"] == 60
    assert db.commit.called


def test_log_tool_call_swallows_db_failure() -> None:
    db = MagicMock()
    db.execute.side_effect = RuntimeError("connection refused")
    entry = ToolCallLog(
        conversation_id="x",
        user_msg_id=None,
        tool_name="get_forecast",
        args={},
        latency_ms=10,
        error="boom",
        tokens_in=None,
        tokens_out=None,
    )
    # Must not propagate — chat loop continues even when logging fails.
    log_tool_call(db, entry)
    assert db.rollback.called


def _funnel_db_mock(*, questions: int, total: int, succeeded: int, avg_lat: float | None,
                    sum_in: int, sum_out: int) -> MagicMock:
    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one.return_value = questions

    mappings_result = MagicMock()
    mappings_result.one.return_value = {
        "total": total,
        "succeeded": succeeded,
        "avg_latency": avg_lat,
        "sum_in": sum_in,
        "sum_out": sum_out,
    }

    def execute(_clause, _params):
        # First call counts user messages (.scalar_one()), second pulls funnel
        # row (.mappings().one()). The order is guaranteed by the function.
        if execute.calls == 0:
            execute.calls += 1
            return scalar_result
        result = MagicMock()
        result.mappings.return_value = mappings_result
        return result

    execute.calls = 0  # type: ignore[attr-defined]
    db.execute.side_effect = execute
    return db


def test_funnel_basic_math() -> None:
    db = _funnel_db_mock(
        questions=3, total=4, succeeded=3, avg_lat=125.5, sum_in=1500, sum_out=400
    )
    f = conversation_funnel(db, "conv-1")
    assert f.questions_asked == 3
    assert f.tools_invoked == 4
    assert f.tools_succeeded == 3
    assert f.render_success_rate == pytest.approx(0.75)
    assert f.avg_latency_ms == 125.5
    assert f.total_tokens_in == 1500
    assert f.total_tokens_out == 400


def test_funnel_zero_tools_treats_rate_as_one() -> None:
    """No tool calls yet — rate defaults to 1.0 so a fresh conversation isn't
    flagged as failing on its first dashboard render."""
    db = _funnel_db_mock(
        questions=1, total=0, succeeded=0, avg_lat=None, sum_in=0, sum_out=0
    )
    f = conversation_funnel(db, "conv-empty")
    assert f.tools_invoked == 0
    assert f.render_success_rate == 1.0
    assert f.avg_latency_ms is None


def test_funnel_to_dict_rounds_rate() -> None:
    f = ConversationFunnel(
        conversation_id="x",
        questions_asked=3,
        tools_invoked=7,
        tools_succeeded=4,
        render_success_rate=4 / 7,
        avg_latency_ms=33.333333,
        total_tokens_in=10,
        total_tokens_out=20,
    )
    d = f.to_dict()
    assert d["render_success_rate"] == 0.5714  # rounded to 4 decimals
    assert d["avg_latency_ms"] == 33.3
