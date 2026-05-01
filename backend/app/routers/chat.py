"""
/chat router — Phase 6.

POST /chat                       — start or continue a conversation. SSE stream.
GET  /chat/conversations/{id}    — load a conversation's full history.

The streaming endpoint runs Anthropic's tool-use loop: the model emits text
tokens, then optionally a tool_use block, we run the tool against Postgres,
stuff the tool_result back in, and let the model continue. Tool outputs are
NOT streamed — they're emitted atomically as a single SSE event so the
frontend renders one chart at a time.

SSE event shapes (each `data:` line is JSON):
  {"type":"token","text":"..."}                          — incremental token
  {"type":"tool_call","tool":"get_forecast","args":{}}   — model invoked a tool
  {"type":"tool_result","tool":"get_forecast","result":{...}}  — render-shaped
  {"type":"truncated","message":"..."}                   — assistant turn cut off at max_tokens
  {"type":"persistence_warning","role":"...","message":"..."} — DB write failed; one-shot per stream
  {"type":"done","conversation_id":"..."}                — stream ended
  {"type":"error","message":"..."}                       — fatal mid-stream
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    RateLimitError,
)
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.services.chat_observability import (
    ToolCallLog,
    conversation_funnel,
    log_tool_call,
)
from app.tools import all_definitions, dispatch

log = logging.getLogger("wfm.chat")
router = APIRouter(prefix="/chat", tags=["chat"])


# Module-level singleton — reusing the HTTP client across streams keeps the
# connection pool warm and avoids ~50ms client-construction overhead per call.
# Lazy-init so importing this module doesn't require ANTHROPIC_API_KEY at
# import time (the eval suites import SYSTEM_PROMPT without setting up env).
_anthropic_client: Anthropic | None = None


def _get_anthropic_client() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = Anthropic(api_key=get_settings().anthropic_api_key)
    return _anthropic_client


# Note on retry: the Anthropic SDK builds streaming requests lazily — the
# network round-trip happens at context-manager ENTRY, not at the
# `client.messages.stream(...)` call. By the time entry succeeds we're
# already inside the worker thread, and once any tokens have been emitted
# to the SSE stream we can't roll back. So "retry" for streaming chat is
# best-effort only and we don't implement it here. Transient errors are
# logged with their type, surfaced to the user with a sanitized message,
# and the conversation must be restarted by the user.


async def _dispatch_with_timeout(
    tool_name: str,
    tool_args: dict[str, Any],
    db: Session,
    timeout_s: int,
) -> tuple[dict[str, Any], int]:
    """Dispatch with a hard wall-clock ceiling.

    Returns (result_dict, latency_ms). On timeout the in-flight thread keeps
    running until it finishes naturally — Python doesn't let us kill arbitrary
    threads — but the chat loop is freed to surface an error render and accept
    the next user message. Closes Phase 6 silent-failure gap #3.
    """
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(dispatch, tool_name, tool_args, db),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        latency = int((time.perf_counter() - t0) * 1000)
        return (
            {
                "render": "error",
                "message": (
                    f"{tool_name} did not return within {timeout_s}s. "
                    "The work may still be running in the background."
                ),
                "code": "TOOL_TIMEOUT",
            },
            latency,
        )
    return result, int((time.perf_counter() - t0) * 1000)


SYSTEM_PROMPT = """You are the WFM Copilot, an assistant for contact-center workforce \
management. You translate natural-language questions into tool calls against the \
real WFM database and present results inline.

Operating rules:
- The math is in the tools. Never invent numbers — call a tool.
- Prefer one tool call per user turn. Pick the most specific tool.
- After a tool returns, write a one-sentence summary in plain language. The chart \
  or table renders inline; do not describe it field-by-field.
- When the user references an anomaly id (monospace 16-hex), preserve it exactly \
  in your reply.
- If a tool returns render:'error', say briefly what failed and suggest a next step.

Tone: direct, ops-people register. No hype. No "I'd be happy to help."
"""

MAX_TOOL_ITERATIONS = 6


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str


# --------------------------------------------------------------------------
# Persistence helpers
# --------------------------------------------------------------------------
def _ensure_conversation(db: Session, conversation_id: str | None) -> str:
    if conversation_id:
        existing = db.execute(
            text("SELECT id FROM chat_conversations WHERE id = CAST(:id AS uuid)"),
            {"id": conversation_id},
        ).scalar_one_or_none()
        if existing:
            return str(existing)
    new_id = db.execute(
        text(
            "INSERT INTO chat_conversations DEFAULT VALUES RETURNING id"
        )
    ).scalar_one()
    db.commit()
    return str(new_id)


def _persist_message(
    db: Session,
    conversation_id: str,
    role: str,
    content: Any,
    tool_calls: Any | None = None,
) -> str | None:
    """Best-effort persistence. Logs and continues on DB failure (Gap #1).

    Returns the new message id on success, None on DB failure. Callers that
    need the id (observability uses it as user_msg_id) should treat None as
    "couldn't persist; log without that link."
    """
    try:
        new_id = db.execute(
            text(
                """
                INSERT INTO chat_messages (conversation_id, role, content, tool_calls)
                VALUES (CAST(:cid AS uuid), :role, CAST(:content AS jsonb), CAST(:tc AS jsonb))
                RETURNING id
                """
            ),
            {
                "cid": conversation_id,
                "role": role,
                "content": json.dumps(content),
                "tc": json.dumps(tool_calls) if tool_calls is not None else None,
            },
        ).scalar_one()
        db.commit()
        return str(new_id)
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception(
            "chat_messages insert failed (conv=%s role=%s) — continuing",
            conversation_id,
            role,
        )
        return None


def _load_history(db: Session, conversation_id: str) -> list[dict[str, Any]]:
    """Load prior turns and return them in Anthropic Messages-API shape."""
    rows = (
        db.execute(
            text(
                """
                SELECT role, content, tool_calls
                FROM chat_messages
                WHERE conversation_id = CAST(:cid AS uuid)
                ORDER BY created_at
                """
            ),
            {"cid": conversation_id},
        )
        .mappings()
        .all()
    )
    history: list[dict[str, Any]] = []
    for r in rows:
        if r["role"] == "user":
            history.append({"role": "user", "content": r["content"]})
        elif r["role"] == "assistant":
            content = r["content"]
            if r["tool_calls"]:
                # Reconstruct the assistant turn with tool_use blocks.
                content = r["tool_calls"]
            history.append({"role": "assistant", "content": content})
        elif r["role"] == "tool_result":
            history.append({"role": "user", "content": r["content"]})
    return history


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------
def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode("utf-8")


async def _stream_chat(message: str, conversation_id: str) -> Any:
    """Run the Anthropic tool-use loop, yielding SSE bytes as we go.

    Opens its own DB session because the request-scoped one closes when the
    initial handler returns and the StreamingResponse takes over.
    """
    settings = get_settings()
    tools = all_definitions()

    persistence_warned = False

    def _persist_warning_event(role: str) -> bytes | None:
        """Emit a single persistence_warning per stream — once we've told the
        client, more failures from the same outage would be noise.

        Returns the SSE bytes to yield, or None if we already warned.
        """
        nonlocal persistence_warned
        if persistence_warned:
            return None
        persistence_warned = True
        return _sse(
            {
                "type": "persistence_warning",
                "role": role,
                "message": (
                    "Couldn't save part of this conversation. "
                    "It will work in this tab but won't survive a refresh."
                ),
            }
        )

    with SessionLocal() as db:
        history = _load_history(db, conversation_id)
        history.append({"role": "user", "content": message})
        user_msg_id = _persist_message(db, conversation_id, "user", message)
        if user_msg_id is None:
            evt = _persist_warning_event("user")
            if evt:
                yield evt

        for _iteration in range(MAX_TOOL_ITERATIONS):
            # Stream model turn until stop or tool_use.
            assistant_blocks: list[dict[str, Any]] = []
            stop_reason = None
            iter_tokens_in: int | None = None
            iter_tokens_out: int | None = None

            # Cache the static prefix (system prompt + tool definitions).
            # See _open_stream_with_retry's docstring + the 2026-05-01 audit PR
            # for the full rationale. Cache breakpoint goes on the LAST tool
            # because system alone (~185 tokens) is below the 1024 minimum.
            tools_cached = (
                [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]
                if tools else tools
            )
            try:
                stream_ctx = _get_anthropic_client().messages.stream(
                    model=settings.anthropic_model,
                    system=SYSTEM_PROMPT,
                    tools=tools_cached,
                    messages=history,
                    # 4096 is a deliberate ceiling for the chat loop:
                    # - Most assistant turns are short (a one-sentence summary
                    #   after a tool result). 1024 covers that easily.
                    # - Tool-use turns can be longer when the model reasons
                    #   before calling a tool. 2048 was hit occasionally per
                    #   the audit. 4096 leaves headroom without paying for
                    #   max_tokens we never use.
                    # - If we ever see `stop_reason: "max_tokens"` in
                    #   production, the explicit handler below surfaces a
                    #   `truncated` SSE event so it's visible, not silent.
                    max_tokens=4096,
                )
            except (RateLimitError, APIConnectionError, APIStatusError) as exc:
                # Anthropic SDK builds the stream lazily, so most transient
                # errors won't surface here — they'll surface inside _drain
                # when we enter the context. This catch is for the rare
                # construction-time failure (e.g. invalid kwargs).
                log.exception(
                    "Anthropic stream construction failed: %s",
                    type(exc).__name__,
                )
                yield _sse(
                    {
                        "type": "error",
                        "message": (
                            "The model service is temporarily unavailable. "
                            "Please retry in a moment."
                        ),
                    }
                )
                return
            except Exception as exc:  # noqa: BLE001 — terminal errors (auth, validation)
                log.exception("Anthropic stream construction failed: %s", type(exc).__name__)
                yield _sse(
                    {
                        "type": "error",
                        "message": "The model service rejected the request.",
                    }
                )
                return

            # The Anthropic SDK's `stream` returns a context manager. Run it
            # in a worker thread because we're an async generator.
            tokens_q: asyncio.Queue[Any] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def _drain(stream_ctx: Any = stream_ctx) -> None:
                try:
                    with stream_ctx as stream:
                        for event in stream:
                            etype = getattr(event, "type", None)
                            if etype == "text":
                                loop.call_soon_threadsafe(
                                    tokens_q.put_nowait, ("token", event.text)
                                )
                        final = stream.get_final_message()
                        loop.call_soon_threadsafe(
                            tokens_q.put_nowait, ("final", final)
                        )
                except (RateLimitError, APIConnectionError, APIStatusError) as exc:
                    # Transient API error — most likely place to land for
                    # rate-limits or connection drops since the SDK is lazy.
                    # Tokens may have already gone out; we can't retry.
                    log.exception(
                        "Anthropic stream transient error mid-flight: %s",
                        type(exc).__name__,
                    )
                    loop.call_soon_threadsafe(
                        tokens_q.put_nowait,
                        ("error", "The model service is temporarily unavailable. Please retry."),
                    )
                except Exception as exc:  # noqa: BLE001
                    # Anything else mid-stream — log full detail, sanitize for client.
                    log.exception(
                        "Anthropic stream errored mid-flight: %s",
                        type(exc).__name__,
                    )
                    loop.call_soon_threadsafe(
                        tokens_q.put_nowait,
                        ("error", "The model stream was interrupted."),
                    )

            asyncio.create_task(asyncio.to_thread(_drain))

            while True:
                kind, payload = await tokens_q.get()
                if kind == "token":
                    yield _sse({"type": "token", "text": payload})
                elif kind == "error":
                    yield _sse({"type": "error", "message": payload})
                    return
                elif kind == "final":
                    final = payload
                    assistant_blocks = [
                        _block_to_dict(b) for b in final.content
                    ]
                    stop_reason = final.stop_reason
                    iter_tokens_in = getattr(final.usage, "input_tokens", None)
                    iter_tokens_out = getattr(final.usage, "output_tokens", None)
                    break

            # Persist the assistant turn (text + any tool_use blocks).
            assistant_text = "".join(
                b["text"] for b in assistant_blocks if b.get("type") == "text"
            )
            assistant_id = _persist_message(
                db,
                conversation_id,
                "assistant",
                assistant_text,
                tool_calls=assistant_blocks,
            )
            if assistant_id is None:
                evt = _persist_warning_event("assistant")
                if evt:
                    yield evt
            history.append({"role": "assistant", "content": assistant_blocks})

            if stop_reason == "max_tokens":
                # The model wanted more space than we gave it. The text we
                # already emitted is partial — the user sees a sentence cut
                # off. Surface a distinct event so the frontend can render a
                # "response truncated" indicator separately from a clean
                # finish. Persisting still happened above, so the
                # conversation continues; the user can ask a follow-up.
                log.warning(
                    "Chat hit max_tokens (conversation=%s, iteration=%d). "
                    "Consider raising max_tokens or shortening prompts.",
                    conversation_id,
                    _iteration,
                )
                yield _sse(
                    {
                        "type": "truncated",
                        "message": (
                            "The reply was cut off because it ran long. "
                            "Ask a follow-up to continue."
                        ),
                    }
                )
                yield _sse({"type": "done", "conversation_id": conversation_id})
                return

            if stop_reason != "tool_use":
                yield _sse({"type": "done", "conversation_id": conversation_id})
                return

            # Run each tool_use block, stuff results back, loop.
            tool_results: list[dict[str, Any]] = []
            for block in assistant_blocks:
                if block.get("type") != "tool_use":
                    continue
                tool_name: str = block["name"]
                tool_args: dict[str, Any] = block.get("input", {})
                yield _sse(
                    {"type": "tool_call", "tool": tool_name, "args": tool_args}
                )
                result, latency_ms = await _dispatch_with_timeout(
                    tool_name, tool_args, db, settings.tool_timeout_seconds
                )
                error: str | None = None
                if isinstance(result, dict) and result.get("render") == "error":
                    error = str(result.get("message") or "tool error")
                log_tool_call(
                    db,
                    ToolCallLog(
                        conversation_id=conversation_id,
                        user_msg_id=user_msg_id,
                        tool_name=tool_name,
                        args=tool_args,
                        latency_ms=latency_ms,
                        error=error,
                        tokens_in=iter_tokens_in,
                        tokens_out=iter_tokens_out,
                    ),
                )
                yield _sse(
                    {"type": "tool_result", "tool": tool_name, "result": result}
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": json.dumps(result),
                    }
                )

            history.append({"role": "user", "content": tool_results})
            tool_result_id = _persist_message(
                db, conversation_id, "tool_result", tool_results
            )
            if tool_result_id is None:
                evt = _persist_warning_event("tool_result")
                if evt:
                    yield evt

        # Hit the iteration cap — emit a final done so the frontend closes.
        yield _sse(
            {
                "type": "error",
                "message": (
                    f"Exceeded {MAX_TOOL_ITERATIONS} tool iterations without a "
                    "final answer."
                ),
            }
        )
        yield _sse({"type": "done", "conversation_id": conversation_id})


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Anthropic SDK block → JSON-serialisable dict."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    # Fallback for unknown block types.
    return {"type": btype or "unknown", "raw": str(block)}


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@router.post("")
async def post_chat(req: ChatRequest, db: Session = Depends(get_db)) -> StreamingResponse:
    conversation_id = _ensure_conversation(db, req.conversation_id)

    return StreamingResponse(
        _stream_chat(req.message, conversation_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            "X-Conversation-Id": conversation_id,
        },
    )


@router.get("/conversations/{conversation_id}/funnel")
def get_conversation_funnel(
    conversation_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    exists = db.execute(
        text("SELECT 1 FROM chat_conversations WHERE id = CAST(:id AS uuid)"),
        {"id": conversation_id},
    ).scalar_one_or_none()
    if not exists:
        raise HTTPException(404, "Conversation not found")
    return conversation_funnel(db, conversation_id).to_dict()


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    rows = (
        db.execute(
            text(
                """
                SELECT id, role, content, tool_calls, created_at
                FROM chat_messages
                WHERE conversation_id = CAST(:cid AS uuid)
                ORDER BY created_at
                """
            ),
            {"cid": conversation_id},
        )
        .mappings()
        .all()
    )
    if not rows:
        # Conversation row might exist with no messages yet, or might not
        # exist at all. Both look the same to the client; 404 if neither.
        exists = db.execute(
            text("SELECT 1 FROM chat_conversations WHERE id = CAST(:id AS uuid)"),
            {"id": conversation_id},
        ).scalar_one_or_none()
        if not exists:
            raise HTTPException(404, "Conversation not found")
    return {
        "conversation_id": conversation_id,
        "messages": [
            {
                "id": str(r["id"]),
                "role": r["role"],
                "content": r["content"],
                "tool_calls": r["tool_calls"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ],
    }
