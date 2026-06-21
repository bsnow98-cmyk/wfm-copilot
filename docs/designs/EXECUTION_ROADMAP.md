# Execution Roadmap — giving the copilot more ways to *act*

**Status:** Draft 2026-06-20. Sequences the write/execution surfaces beyond schedule-segment edits, and specs the first one (leave approval) in implementation detail.

**Source:** Follows the proven pattern in [`CHAT_WRITE_ACTIONS.md`](CHAT_WRITE_ACTIONS.md) (cherry-pick D, shipped). That doc is the *mechanism*; this doc is the *backlog + sequencing*.

---

## The principle (do not skip this)

Today the copilot has **40 tools and none of them mutate data** — it can read and *recommend* the whole job (6 `recommend_*` tools, `preview_schedule_change`) but can only *execute* one thing: schedule-segment edits, and only behind an explicit human click.

That is deliberate, and it stays:

> **The LLM proposes; a human commits. Write endpoints are never in the tool registry.**

Every execution skill below is a **preview→apply pair**, not an autonomous LLM write:

1. A **read-only preview tool** (in the registry) computes the proposed change + its consequences and mints a single-use `apply_token`.
2. A **UI affordance** renders an Apply button only when the token is present.
3. A **mutation endpoint** (NOT a tool) consumes the token, writes inside one transaction, appends to an **append-only audit log**, and fires a **notification**.
4. **Undo** reverses within a window; **idempotency** via the single-use token; **optimistic concurrency** rejects stale previews.

"More skills to execute the job" = **more preview→apply pairs over more domains.** Letting the LLM write *autonomously* (no human click) is a separate trust-boundary decision — see [§Autonomous execution](#autonomous-execution-explicitly-out-of-scope).

---

## The reusable pattern (template for every surface)

Generalized from cherry-pick D so each new surface is mostly config, not new architecture:

| Layer | Schedule edits (shipped) | What a new surface supplies |
|---|---|---|
| Preview tool | `preview_schedule_change` | a `preview_<x>` tool that returns a render + `apply_token` + `version` |
| Token store | `chat_apply_tokens` | **reused as-is** (token pins the change set + version) |
| Apply endpoint | `POST /schedules/apply` | `POST /<x>/apply` (+ optional `/undo`) |
| Audit log | `schedule_change_log` | a `<x>_change_log` (same shape: before/after JSONB, undo window) |
| Notification | `schedule_applied` / `schedule_undone` | new categories on the existing `notifications` table |
| Concurrency | `schedule_version` hash | a version/etag over the affected rows |

**Cross-cutting work worth doing once (before surface #3):** extract a small `app/services/write_actions.py` that owns token-consume → audit-insert → notify → commit, so each new endpoint is ~30 lines. Today that flow is hand-rolled in `schedule_changes.py`; a second copy is fine, a third means extract.

---

## Sequenced backlog

Ordered by *leverage ÷ effort*, with dependencies. Leverage = "demo a WFM manager immediately gets."

| # | Write surface | What the AI can now execute | Leverage | Effort | Notes / deps |
|---|---|---|---|---|---|
| **1** | **Leave approval** ✅ **shipped** | Approve / deny a pending leave request from chat | **High** | **S–M** | Specced below. Shipped: migration `0018`, `preview_leave_decision` tool, `POST /leave/decisions/apply` + `/undo` + audit feed, `leave_decision_log`, `leave_decided`/`leave_decision_undone` notifications, TableRenderer Approve/Deny affordance. Token store generalized via `target_kind` (not the deferred `target_id` pair — that lands with #2). |
| 2 | **OT / VTO offer publish** ✅ **shipped** | Post an OT or VTO offer to a target group | High | M | Shipped: migration `0019_offers.sql`, `preview_offer` tool (reuses recommend_ot/vto window math), `POST /offers/apply` + `/{id}/retract` + audit feed, `offers` table (own audit record; status open→retracted, 24h window), `offer_published`/`offer_retracted` notifications, TableRenderer Publish affordance. Publish-only — no agent-accept loop yet (v2). v1 offers to the recommended group (custom recipient lists = v2). **Tech debt:** preview_offer duplicates recommend_ot/vto's candidate SQL — fold into the `write_actions.py` extraction before #3. |
| 3 | Break / activity move (generalized) | Move a break or swap an activity for one agent | Medium | S | `recommend_break_shift` exists and the *write* path already exists — this reuses `POST /schedules/apply` with a recommend-sourced change set. Mostly a preview-tool wrapper. |
| 4 | **`apply_forecast_override`** ✅ **shipped** | Pin a forecast interval to an analyst value | Medium | M | Shipped on the shared envelope: migration `0020_forecast_override_log.sql`, `preview_forecast_override` tool, `POST /forecast/overrides/apply` + `/{log_id}/undo` + audit feed, `forecast_override_applied`/`_undone` notifications, TableRenderer Override affordance. Optimistic concurrency on the interval value (409 if re-run/re-overridden). **Downstream staffing recompute is deferred** (a job — pairs with #5); the preview says "recompute staffing separately". |
| 5 | `apply_staffing_target` | Change an SL/ASA target for a window | Medium | M | Same as #4; recomputing staffing is a *job*, not a sync write — pairs with the async solver. |
| 6 | Shift creation / roster add | Create a new shift / add an agent to a day | Lower | L | Biggest blast radius; do last, after RBAC. |

**Gating dependency for breadth:** multi-user **RBAC** (TODOS, v2). Surfaces 1–4 are safe under the single-password gate because every write is human-clicked and audited with `decided_by='demo'`. Surfaces 5–6 and any *autonomous* execution want real identities first.

---

## Surface #1 spec — Leave approval

### Goal

After `recommend_leave_approval` lists the pending queue with verdicts, the user can **approve or deny a specific request inline** — the copilot previews the SL impact, the user clicks, the decision is written, audited, and announced.

### Scope (v1)

- **In:** approve / deny a single pending `leave_requests` row; SL-impact preview (reuse `check_leave_feasibility`); audit log; notification; undo within 24h; idempotency; optimistic concurrency on the request's status.
- **Out:** bulk "approve all APPROVE-verdict rows" (v2 — one confirm per write keeps the audit clean); agent-facing accept/decline; auto-decrement of the PTO *balance* beyond a ledger row (see §PTO note); cross-conversation undo.

### Flow

| Step | Code | Notes |
|---|---|---|
| Recommend queue | `recommend_leave_approval` (exists) | Table includes `Req ID` + verdict per request |
| Preview a decision | **new** `preview_leave_decision` tool | Input `{request_id, decision: 'approve'\|'deny', note?}`. Reuses `check_leave_feasibility` math for the affected days, returns a `table` render + `apply_token` + `request_version`. Read-only. |
| Click Approve/Deny | **new** UI affordance on the leave table render | Renders only when `apply_token` present; two buttons (Approve = primary, Deny = secondary) |
| Apply | **new** `POST /leave/decisions/apply` | Consumes token; flips `status`; sets `decided_at`/`decided_by='demo'`/`decision_note`; on approve writes a `pto_ledger` `use` row; appends `leave_decision_log`; fires `leave_decided` notification; all in one txn |
| Undo | **new** `POST /leave/decisions/{log_id}/undo` | Within 24h: restore `status='pending'`, null the decision fields, reverse the ledger row, write `leave_decision_undone` |

### New endpoints

```http
POST /leave/decisions/apply
  body: { apply_token: string }            # token pins request_id + decision + version
  200:  { log_id: uuid, request_id: int, status: 'approved'|'denied', decided_at: iso }
  404:  apply_token not found
  409:  { current_version, your_version, fresh_preview }   # request already decided/changed
  410:  token expired (idempotent re-apply returns the original 200)

POST /leave/decisions/{log_id}/undo
  200:  { undo_log_id: uuid, undone_at: iso }
  409:  outside 24h window / already undone

GET  /leave/decisions?since=&limit=        # audit feed (mirrors /schedules/changes)
```

### New migration — `0018_leave_decision_log.sql`

```sql
CREATE TABLE IF NOT EXISTS leave_decision_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_by      TEXT NOT NULL DEFAULT 'demo',
    conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
    request_id      BIGINT NOT NULL REFERENCES leave_requests(id) ON DELETE CASCADE,
    decision        TEXT NOT NULL CHECK (decision IN ('approve','deny')),
    before_state    JSONB NOT NULL,   -- {status, decided_at, decided_by, decision_note}
    after_state     JSONB NOT NULL,
    ledger_event_id BIGINT REFERENCES pto_ledger(id) ON DELETE SET NULL,  -- the 'use' row, if approve
    undo_window_ends_at TIMESTAMPTZ NOT NULL,
    undone_at       TIMESTAMPTZ,
    undone_by_log_id UUID REFERENCES leave_decision_log(id) ON DELETE SET NULL
);
```

`chat_apply_tokens` is reused unchanged — `change_set` holds `{request_id, decision, note}`; `schedule_id` is nullable-overloaded or we add a generic `target_kind`/`target_id` pair (cheap migration, do it when surface #2 lands so the token store stops being schedule-specific).

### The five hidden costs (decisions, mirroring cherry-pick D)

1. **Auth / injection** — `preview_leave_decision` is the only registry tool; it mints the token, it cannot decide. The LLM can recommend and preview, never write. ✔ same defense as D.
2. **Audit** — `leave_decision_log`, append-only, before/after snapshots, 24h undo. A denied-then-undone request leaves three rows (decide, undo) and a readable history.
3. **Dry-run** — the preview *is* the `check_leave_feasibility` table (per-day required vs available, OK/WARN/FAIL). The Approve button sits under it; server-side `summarize_decision()` writes the one-liner ("Approved PTO for Adams, 06-12→06-14 — worst-day margin +3").
4. **Idempotency** — single-use token; duplicate apply returns the original `log_id`.
5. **Concurrency** — `request_version` = hash of `(status, decided_at)`. If a manager decided the same request in the UI between preview and apply → 409 with a fresh preview. No silent overwrite of someone else's decision.

### PTO note

Approving consumes PTO. v1 writes a `pto_ledger` `use` row (negative hours, `balance_after` computed) so `get_pto_balance` stays correct, and undo reverses it. v1 does **not** block approval on insufficient balance — it *warns* in the preview (consistent with "the AI shows its math, the human decides"). Hard-blocking on balance is a v2 policy toggle.

### Acceptance criteria

- Approve flips `status`→`approved`, sets decision fields, writes one `pto_ledger` use row + one `leave_decision_log` row + one `leave_decided` notification.
- Deny flips `status`→`denied`, no ledger row, log + notification still written.
- Re-apply with the same token → original `log_id` (idempotent).
- Apply against an already-decided request → 409 with both versions.
- Undo within 24h restores `pending` + nulls decision fields + reverses the ledger row; outside 24h → 409.
- `get_pto_balance` and `get_leave_requests` reflect the change immediately; undo restores both.

### Phasing (≈ same size as cherry-pick D's happy path)

1. `0018_leave_decision_log.sql`.
2. `preview_leave_decision` tool (reuses `check_leave_feasibility`; mints token).
3. `POST /leave/decisions/apply` (consume → flip status → ledger → audit → notify → commit).
4. `summarize_decision()` server-side helper.
5. `leave_decided` / `leave_decision_undone` notification categories.
6. Undo endpoint + ledger reversal.
7. Frontend: Approve/Deny affordance on the leave-table render + post-decision confirmation; reuse `NotificationBell`.
8. Tests: idempotency, 409, undo, undo-outside-window, ledger round-trip, notification counts — same matrix as `test_schedule_apply*.py`.

---

## Autonomous execution (explicitly out of scope)

Everything above keeps a **human click on every write**. Removing the click — letting the copilot decide *and* execute (e.g., "auto-approve all comfortable-margin leave overnight") — is a different product with a different risk profile. It is **not** "add more tools"; it would require, at minimum:

- **RBAC + real identities** (so `applied_by` is a person/policy, not `'demo'`).
- **Policy objects** the AI executes against (named, versioned, auditable — e.g. "auto-approve margin ≥ 4, else queue").
- **A kill switch + rate limits** on autonomous writes, and a daily digest of what it did.
- **An audience decision** — WFM managers may *want* this; portfolio reviewers may read it as reckless. Resolve before building.

Park it here so it's a deliberate future choice, not a slippery default.

## See also

- [`CHAT_WRITE_ACTIONS.md`](CHAT_WRITE_ACTIONS.md) — the shipped mechanism this generalizes.
- `~/Desktop/Projects/wfm-copilot-vault/TODOS.md` — RBAC, Slack surface, ACD integration.
- `backend/app/tools/recommend_leave_approval.py`, `check_leave_feasibility.py` — the read side surface #1 builds on.
- `backend/app/routers/schedule_changes.py` — the apply/undo router to mirror.
