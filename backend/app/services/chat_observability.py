"""
Chat observability — log every tool_use round-trip and compute per-conversation
funnels.

The funnel answers three questions about a conversation:
- How many user questions were asked?
- How many tool calls did the model make?
- What fraction of those calls produced a typed render (vs render:'error')?

Render success is the metric that catches silent regressions: a sudden drop
means the model is invoking tools with bad args, or a service downstream is
returning errors that fell back to render:'error'.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("wfm.chat.observability")


@dataclass(frozen=True)
class ToolCallLog:
    conversation_id: str
    user_msg_id: str | None
    tool_name: str
    args: dict[str, Any]
    latency_ms: int
    error: str | None
    tokens_in: int | None
    tokens_out: int | None


def log_tool_call(db: Session, entry: ToolCallLog) -> None:
    """Best-effort insert. A logging failure must never break the chat loop —
    the same pattern as _persist_message in the chat router (silent-failure
    gap #1: convert silence into a log line, not a 500)."""
    try:
        db.execute(
            text(
                """
                INSERT INTO chat_tool_calls
                    (conversation_id, user_msg_id, tool_name, args_json,
                     latency_ms, error, tokens_in, tokens_out)
                VALUES
                    (CAST(:cid AS uuid), :umid, :tool, CAST(:args AS jsonb),
                     :latency, :error, :tin, :tout)
                """
            ),
            {
                "cid": entry.conversation_id,
                "umid": entry.user_msg_id,
                "tool": entry.tool_name,
                "args": json.dumps(entry.args),
                "latency": entry.latency_ms,
                "error": entry.error,
                "tin": entry.tokens_in,
                "tout": entry.tokens_out,
            },
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception(
            "chat_tool_calls insert failed (conv=%s tool=%s) — continuing",
            entry.conversation_id,
            entry.tool_name,
        )


@dataclass
class ConversationFunnel:
    conversation_id: str
    questions_asked: int
    tools_invoked: int
    tools_succeeded: int
    render_success_rate: float
    avg_latency_ms: float | None
    total_tokens_in: int
    total_tokens_out: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "questions_asked": self.questions_asked,
            "tools_invoked": self.tools_invoked,
            "tools_succeeded": self.tools_succeeded,
            "render_success_rate": round(self.render_success_rate, 4),
            "avg_latency_ms": (
                round(self.avg_latency_ms, 1)
                if self.avg_latency_ms is not None
                else None
            ),
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
        }


def conversation_funnel(db: Session, conversation_id: str) -> ConversationFunnel:
    questions = db.execute(
        text(
            """
            SELECT COUNT(*) FROM chat_messages
            WHERE conversation_id = CAST(:cid AS uuid) AND role = 'user'
            """
        ),
        {"cid": conversation_id},
    ).scalar_one()

    row = db.execute(
        text(
            """
            SELECT
                COUNT(*)                                   AS total,
                SUM((error IS NULL)::int)                  AS succeeded,
                AVG(latency_ms)                            AS avg_latency,
                COALESCE(SUM(tokens_in), 0)                AS sum_in,
                COALESCE(SUM(tokens_out), 0)               AS sum_out
            FROM chat_tool_calls
            WHERE conversation_id = CAST(:cid AS uuid)
            """
        ),
        {"cid": conversation_id},
    ).mappings().one()

    total = int(row["total"] or 0)
    succeeded = int(row["succeeded"] or 0)
    rate = (succeeded / total) if total else 1.0

    return ConversationFunnel(
        conversation_id=conversation_id,
        questions_asked=int(questions),
        tools_invoked=total,
        tools_succeeded=succeeded,
        render_success_rate=rate,
        avg_latency_ms=float(row["avg_latency"]) if row["avg_latency"] is not None else None,
        total_tokens_in=int(row["sum_in"]),
        total_tokens_out=int(row["sum_out"]),
    )
