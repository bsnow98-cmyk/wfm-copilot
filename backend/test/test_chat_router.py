"""
Streaming-loop regression tests for app/routers/chat.py.

The chat router is the most complex untested code path: SSE event ordering,
persistence hooks, observability, the timeout wrapper, and the persistence-
warning event all flow through `_stream_chat`. These tests mock the Anthropic
client and the DB so they run without an API key or Postgres.

The Anthropic SDK's `.messages.stream()` returns a context manager that yields
event objects and exposes `.get_final_message()`. We replace it with a small
fake that produces a canned sequence — one token plus a tool_use block, then
a final stop turn — so we can assert the SSE event ordering and contents.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

from app.routers import chat as chat_router


# --------------------------------------------------------------------------
# Fakes for the Anthropic streaming surface.
# --------------------------------------------------------------------------
class _FakeStream:
    """Stands in for the iterator the Anthropic context manager yields."""

    def __init__(self, events: list[Any], final_message: Any) -> None:
        self._events = events
        self._final = final_message

    def __iter__(self) -> Iterator[Any]:
        return iter(self._events)

    def get_final_message(self) -> Any:
        return self._final


class _FakeStreamCtx:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    def __enter__(self) -> _FakeStream:
        return self._stream

    def __exit__(self, *args: Any) -> bool:
        return False


def _text_event(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, input_: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id="t-1", name=name, input=input_)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _final(blocks: list[Any], stop_reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=120, output_tokens=60),
    )


def _build_two_turn_client() -> MagicMock:
    """Round 1: text + tool_use(get_forecast). Round 2: text, stop."""
    streams = [
        _FakeStreamCtx(
            _FakeStream(
                events=[_text_event("Pulling")],
                final_message=_final(
                    blocks=[
                        _text_block("Pulling"),
                        _tool_use_block("get_forecast", {"queue": "sales_inbound"}),
                    ],
                    stop_reason="tool_use",
                ),
            )
        ),
        _FakeStreamCtx(
            _FakeStream(
                events=[_text_event(" Forecast")],
                final_message=_final(
                    blocks=[_text_block(" Forecast")],
                    stop_reason="end_turn",
                ),
            )
        ),
    ]
    client = MagicMock()
    client.messages.stream.side_effect = streams
    return client


def _drain_stream(message: str, conversation_id: str) -> list[dict[str, Any]]:
    """Run _stream_chat to completion, return the parsed SSE event list."""

    async def go() -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for chunk in chat_router._stream_chat(message, conversation_id):
            text = chunk.decode("utf-8")
            for line in text.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: ") :]))
        return events

    return asyncio.run(go())


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_happy_path_emits_token_toolcall_toolresult_done_in_order() -> None:
    fake_db_ctx = MagicMock()
    fake_db = MagicMock()
    fake_db_ctx.__enter__.return_value = fake_db
    fake_db_ctx.__exit__.return_value = False

    forecast_render = {
        "render": "chart.line",
        "title": "Forecast",
        "series": [],
    }

    with (
        patch.object(chat_router, "Anthropic", return_value=_build_two_turn_client()),
        patch.object(chat_router, "SessionLocal", return_value=fake_db_ctx),
        patch.object(chat_router, "_load_history", return_value=[]),
        patch.object(chat_router, "_persist_message", return_value="msg-1"),
        patch.object(chat_router, "log_tool_call"),
        patch.object(chat_router, "dispatch", return_value=forecast_render),
    ):
        events = _drain_stream("show me today's forecast", "conv-1")

    types = [e["type"] for e in events]
    # First three events from round 1: token, tool_call, tool_result.
    # Round 2 emits a token, then done. Order matters here — the renderer
    # depends on tool_result arriving before the next assistant text/done.
    assert types[0] == "token" and events[0]["text"] == "Pulling"
    tc_idx = types.index("tool_call")
    tr_idx = types.index("tool_result")
    done_idx = types.index("done")
    assert tc_idx < tr_idx < done_idx

    tool_call = events[tc_idx]
    assert tool_call["tool"] == "get_forecast"
    assert tool_call["args"] == {"queue": "sales_inbound"}

    tool_result = events[tr_idx]
    assert tool_result["tool"] == "get_forecast"
    assert tool_result["result"] == forecast_render

    assert events[done_idx]["conversation_id"] == "conv-1"


def test_persistence_failure_emits_warning_event_once() -> None:
    """When _persist_message returns None, _stream_chat must emit exactly one
    persistence_warning event for the whole stream — not one per failed write."""
    fake_db_ctx = MagicMock()
    fake_db = MagicMock()
    fake_db_ctx.__enter__.return_value = fake_db
    fake_db_ctx.__exit__.return_value = False

    with (
        patch.object(chat_router, "Anthropic", return_value=_build_two_turn_client()),
        patch.object(chat_router, "SessionLocal", return_value=fake_db_ctx),
        patch.object(chat_router, "_load_history", return_value=[]),
        # Always return None to simulate a wedged DB.
        patch.object(chat_router, "_persist_message", return_value=None),
        patch.object(chat_router, "log_tool_call"),
        patch.object(
            chat_router,
            "dispatch",
            return_value={"render": "text", "content": "ok"},
        ),
    ):
        events = _drain_stream("hi", "conv-warn")

    warnings = [e for e in events if e["type"] == "persistence_warning"]
    assert len(warnings) == 1, (
        f"expected exactly one persistence_warning, got {len(warnings)}: "
        "stream must dedupe so a single outage doesn't spam the user."
    )
    assert "save" in warnings[0]["message"].lower()


def test_anthropic_stream_error_emits_error_event_and_stops() -> None:
    fake_db_ctx = MagicMock()
    fake_db = MagicMock()
    fake_db_ctx.__enter__.return_value = fake_db
    fake_db_ctx.__exit__.return_value = False

    failing_client = MagicMock()
    failing_client.messages.stream.side_effect = RuntimeError("bad key")

    with (
        patch.object(chat_router, "Anthropic", return_value=failing_client),
        patch.object(chat_router, "SessionLocal", return_value=fake_db_ctx),
        patch.object(chat_router, "_load_history", return_value=[]),
        patch.object(chat_router, "_persist_message", return_value="msg-1"),
        patch.object(chat_router, "log_tool_call"),
        patch.object(chat_router, "dispatch"),
    ):
        events = _drain_stream("hi", "conv-error")

    assert any(
        e["type"] == "error" and "bad key" in e["message"] for e in events
    ), f"expected error event with the underlying message, got {events}"
    # After an error we stop — no done event, no further turns.
    assert not any(e["type"] == "done" for e in events)


@pytest.mark.parametrize(
    "render_value,expected_error",
    [
        ({"render": "text", "content": "ok"}, None),
        ({"render": "error", "message": "boom", "code": "X"}, "boom"),
    ],
)
def test_log_tool_call_records_error_only_for_error_render(
    render_value: dict[str, Any], expected_error: str | None
) -> None:
    """Observability: render:'error' tool results should set the error column;
    typed renders should leave it null."""
    fake_db_ctx = MagicMock()
    fake_db = MagicMock()
    fake_db_ctx.__enter__.return_value = fake_db
    fake_db_ctx.__exit__.return_value = False

    captured: list[chat_router.ToolCallLog] = []

    def capture(_db: object, entry: chat_router.ToolCallLog) -> None:
        captured.append(entry)

    with (
        patch.object(chat_router, "Anthropic", return_value=_build_two_turn_client()),
        patch.object(chat_router, "SessionLocal", return_value=fake_db_ctx),
        patch.object(chat_router, "_load_history", return_value=[]),
        patch.object(chat_router, "_persist_message", return_value="msg-1"),
        patch.object(chat_router, "log_tool_call", side_effect=capture),
        patch.object(chat_router, "dispatch", return_value=render_value),
    ):
        _drain_stream("anything", "conv-log")

    assert len(captured) == 1
    assert captured[0].error == expected_error
    assert captured[0].tool_name == "get_forecast"
    assert captured[0].tokens_in == 120
    assert captured[0].tokens_out == 60
