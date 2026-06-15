"""
Daily briefing — the copilot's first proactive surface.

Pulls the morning picture from the same tools the chat copilot uses
(daily summary, anomalies, top risks, intraday gaps), has the model compose
a short supervisor briefing, and drops it into the notifications feed.
Triggered by POST /notifications/daily-briefing (see the GitHub Actions cron
in .github/workflows/daily-briefing.yml). Idempotent per real calendar day,
so cron retries can't double-post.

The composer gets ONLY tool JSON and is instructed to use only those
numbers — same faithfulness contract as chat (checked by
test/eval_faithfulness.py).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.notifications import Notification, get_default_sink
from app.services.realtime_clock import sim_today

log = logging.getLogger("wfm.daily_briefing")

CATEGORY = "daily_briefing"

_SOURCES: list[tuple[str, dict[str, Any]]] = [
    ("get_daily_summary", {}),
    ("get_anomalies", {}),
    ("get_top_risks", {}),
    ("get_intraday_gaps", {}),
]

COMPOSER_SYSTEM = """You write the 7am briefing for a contact-center \
supervisor. You get raw JSON tool results from the WFM database. Write \
80-140 words, plain text, ops register, no hype, no greeting. Lead with the \
single most important thing. Use ONLY numbers present in the tool results — \
never estimate or invent. If a section's data is empty or errored, skip it \
silently. End with the one action you'd take first today."""


def _already_sent_today(db: Session) -> bool:
    return bool(
        db.execute(
            text(
                "SELECT 1 FROM notifications "
                "WHERE category = :cat AND created_at::date = CURRENT_DATE "
                "LIMIT 1"
            ),
            {"cat": CATEGORY},
        ).scalar_one_or_none()
    )


def generate_daily_briefing(db: Session, *, force: bool = False) -> dict[str, Any]:
    """Compose + post today's briefing. Returns a small status dict."""
    if not force and _already_sent_today(db):
        return {"generated": False, "reason": "already sent today"}

    from app.tools import dispatch  # late import — avoids registry import cost at boot

    gathered: list[dict[str, Any]] = []
    for tool_name, args in _SOURCES:
        out = dispatch(tool_name, args, db)
        db.rollback()  # tools never commit; clear any failed-query state
        gathered.append({"tool": tool_name, "result": out})

    from anthropic import Anthropic

    from app.config import get_settings

    settings = get_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        system=COMPOSER_SYSTEM,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": json.dumps(gathered, default=str)[:40000],
        }],
    )
    briefing = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not briefing:
        return {"generated": False, "reason": "composer returned no text"}

    today = sim_today(db)
    notification_id = get_default_sink().send(
        db,
        Notification(
            category=CATEGORY,
            source="daily_briefing",
            payload={
                "render": "text",
                "title": f"Morning briefing — {today.isoformat()}",
                "content": briefing,
            },
        ),
    )
    db.commit()
    log.info("Daily briefing posted (notification=%s)", notification_id)
    return {"generated": True, "notification_id": notification_id, "briefing": briefing}
