"""
Phase 6 GA gap #3 — solver timeout enforcement.

Asserts _dispatch_with_timeout caps wall-clock at the configured ceiling and
returns a typed render:'error' instead of hanging the chat loop.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

from app.routers import chat as chat_router


def test_timeout_returns_error_render_at_ceiling() -> None:
    """A handler that hangs is freed at the configured ceiling.

    The reported latency is the chat-loop-perceived time; it must be ~timeout,
    not the full hang duration. (asyncio.run blocks on default-executor
    shutdown waiting for the leaked thread to finish, but the chat loop
    itself yielded the error render at t=timeout — that's what we assert.)
    """

    def slow_dispatch(_name: str, _args: dict, _db: object) -> dict:
        time.sleep(2.0)
        return {"render": "text", "content": "would-be result"}

    with patch.object(chat_router, "dispatch", side_effect=slow_dispatch):
        result, latency = asyncio.run(
            chat_router._dispatch_with_timeout(
                "get_schedule", {"date": "2026-04-29"}, MagicMock(), timeout_s=1
            )
        )

    assert result["render"] == "error"
    assert result["code"] == "TOOL_TIMEOUT"
    assert "get_schedule" in result["message"]
    # Latency reflects when the chat loop saw the error, not when the leaked
    # thread finished. Should be ~1000ms, well below the 2s sleep.
    assert 950 <= latency <= 1300, (
        f"latency={latency}ms: chat loop freed too late (or absurdly early). "
        "This means the timeout wrapper isn't firing at the configured ceiling."
    )


def test_fast_dispatch_returns_underlying_result() -> None:
    """When the handler finishes quickly, the timeout wrapper is transparent."""

    def fast_dispatch(_name: str, _args: dict, _db: object) -> dict:
        return {"render": "text", "content": "fast"}

    with patch.object(chat_router, "dispatch", side_effect=fast_dispatch):
        result, latency = asyncio.run(
            chat_router._dispatch_with_timeout(
                "get_forecast", {"queue": "x"}, MagicMock(), timeout_s=30
            )
        )
    assert result == {"render": "text", "content": "fast"}
    assert latency < 200  # generous; the call is essentially instant
