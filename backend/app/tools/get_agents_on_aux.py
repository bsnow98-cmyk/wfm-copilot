"""
get_agents_on_aux — who is in a non-productive aux state right now.

Filters to agents currently *in* an aux event whose code is not in
{available, on_call, acw, offline}. Returns name, aux code, how long
they've been in it, and what they were *supposed* to be doing per
shift_segments.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

definition: dict[str, Any] = {
    "name": "get_agents_on_aux",
    "description": (
        "List agents currently in a non-productive aux state (break, lunch, "
        "training, meeting, coaching, system). Shows how long they've been "
        "there and what they were planned to be doing. Use when the user "
        "asks 'who's on break right now', 'who's not on calls', 'who's "
        "currently aux'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "aux_code": {
                "type": "string",
                "description": "Optional filter to a specific aux code (e.g. 'break').",
            }
        },
    },
}


def handler(args: dict[str, Any], db: Session) -> dict[str, Any]:
    aux_code = args.get("aux_code")
    params: dict[str, Any] = {}
    extra = ""
    if aux_code:
        extra = "AND ax.aux_code = :aux_code"
        params["aux_code"] = aux_code

    rows = (
        db.execute(
            text(
                f"""
                WITH now_ts AS (SELECT sim_now() AS ts)
                SELECT a.full_name, a.employee_id, ax.aux_code,
                       ax.start_ts, ax.reason_code,
                       (SELECT seg.segment_type
                          FROM shift_segments seg
                         WHERE seg.agent_id = a.id
                           AND seg.start_time <= (SELECT ts FROM now_ts)
                           AND seg.end_time   >  (SELECT ts FROM now_ts)
                         ORDER BY seg.start_time DESC LIMIT 1) AS planned
                FROM agent_aux_events ax
                JOIN agents a ON a.id = ax.agent_id
                WHERE ax.aux_code NOT IN ('available','on_call','acw','offline')
                  AND ax.start_ts <= (SELECT ts FROM now_ts)
                  AND (ax.end_ts IS NULL OR ax.end_ts > (SELECT ts FROM now_ts))
                  {extra}
                ORDER BY ax.start_ts ASC
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    now = db.execute(text("SELECT sim_now() AS ts")).mappings().one()["ts"]

    table_rows = []
    for r in rows:
        dur = int((now - r["start_ts"]).total_seconds())
        planned = r["planned"] or "off"
        mismatch = (planned == "work") and (r["aux_code"] != "break" and r["aux_code"] != "lunch")
        table_rows.append(
            [
                r["full_name"],
                r["employee_id"],
                r["aux_code"],
                f"{dur // 60}m{dur % 60:02d}s",
                planned,
                "⚠️" if mismatch else "",
                r["reason_code"] or "",
            ]
        )
    return {
        "render": "table",
        "title": f"Agents on aux — {now.strftime('%H:%M:%S')} (sim) — {len(rows)} on aux",
        "columns": ["Agent", "ID", "Aux code", "In aux for", "Planned", "Mismatch?", "Reason"],
        "rows": table_rows,
    }
