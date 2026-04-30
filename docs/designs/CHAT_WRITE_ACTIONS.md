# Chat Write Actions — Design Doc

**Status:** ✅ Accepted 2026-04-29. Decisions on the four open questions locked below. Implementation can begin; one round of `/plan-eng-review` is recommended before merging.

**Source:** `TODOS.md` cherry-pick D in the vault. Promoted from "implement immediately" to "design first" during CEO review because of the five hidden costs below.

**Reviewers:** product owner (locked-in 2026-04-29).

---

## Goal

Close the loop from "AI suggests" to "AI does." After the chat copilot proposes a schedule edit (via `preview_schedule_change`), an inline **Apply** button writes it back through an authenticated mutation endpoint.

This is the demo a recruiter stops scrolling for. It's also the change that turns this codebase from "smart dashboard" into "system that takes action," which is a different operational risk profile.

## Scope

### v1 (in this design)

- One write surface: **schedule segment changes** (move a break, swap an activity, change end time). The same shape as `preview_schedule_change`'s input.
- Apply button rendered inline in the chat scroll, attached to the gantt render that came from `preview_schedule_change`.
- Single-tenant, single-shared-password auth (same gate as the rest of v1).
- Postgres-backed audit log. Undo via reverse-write within an undo window.
- Optimistic concurrency: server rejects writes if the underlying schedule has been edited since the preview was issued.

### Out of scope (deferred — name them, don't sneak them in)

- **Multiple write tools.** No `apply_forecast_override`, no `apply_staffing_target`, no shift-creation. Only schedule segment edits in v1.
- **Multi-user RBAC.** Single-password gate is sufficient for the portfolio target audience. RBAC is a v2 conversation tied to deciding the audience question (TODOS strategic open question #1).
- **Long-running async writes.** The schedule mutation is small enough to be synchronous. If the future write surface includes "re-run CP-SAT solver," that becomes a job model and is a separate phase.
- **Cross-conversation undo.** Undo only works within the conversation that issued the apply. Historical undo from outside the chat is a v2 admin tool.

## The five hidden costs — decisions

Each section: **problem → decision → rationale → what's deferred**.

### 1. Auth — who can apply?

**Problem:** The existing single-password gate authorizes *any* request from a logged-in client. Once a write endpoint exists, that means any caller of the API can mutate schedules, including a tool that the LLM was tricked into calling on the user's behalf.

**Decision:**
- **Apply requires an explicit user click**, never an LLM-initiated call. The chat tool returns a `gantt`-shaped preview *plus* an `apply_token` (opaque, server-issued, single-use, 5-minute TTL).
- The frontend renders the Apply button only when the preview includes an `apply_token`. Clicking sends the token to `POST /schedules/apply` along with the change set.
- The LLM cannot call `/schedules/apply` directly — it's not in the tool registry. Cherry-pick D is *not* a new chat tool; it's a UI affordance built on top of an existing tool's output.

**Rationale:**
- Defense-in-depth against prompt injection: even if a malicious anomaly note says "now call apply_schedule_change with these args," the model has no way to do it.
- Explicit user click is also the right UX: a write that happens silently is a write the user can't recall consenting to.
- Single-use token narrows the time window for replay attacks if the chat session is hijacked.

**Deferred:**
- Per-user RBAC (who can apply *which* changes). Tied to the audience question.
- API-key auth for headless callers. Not needed in v1.

### 2. Audit log — what was applied, by whom, when, for how long undoable?

**Problem:** Without an audit row, we can't answer "who moved Adams's lunch yesterday and why?" — and the spec calls "the AI shows its math" the load-bearing principle. A silent write breaks that.

**Decision:**
- New table `schedule_change_log`:
  ```sql
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid()
  applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
  applied_by      TEXT  NOT NULL  -- session id or user marker; "demo" in v1
  conversation_id UUID  REFERENCES chat_conversations(id) ON DELETE SET NULL
  user_msg_id     UUID  REFERENCES chat_messages(id)      ON DELETE SET NULL
  schedule_id     BIGINT NOT NULL REFERENCES schedules(id) ON DELETE CASCADE
  change_set      JSONB NOT NULL  -- the request payload, exactly as sent
  before_state    JSONB NOT NULL  -- the affected segments before the write
  after_state     JSONB NOT NULL  -- the affected segments after the write
  undo_window_ends_at  TIMESTAMPTZ NOT NULL
  undone_at       TIMESTAMPTZ
  undone_by_log_id     UUID REFERENCES schedule_change_log(id)
  ```
- `undo_window_ends_at = applied_at + 24h` in v1.
- An applied write that's later undone is **never deleted from the log** — undo creates a new log row whose `change_set` is the inverse, and the original row's `undone_at` / `undone_by_log_id` are filled in.
- `GET /schedules/changes?since=...` lists log entries; `POST /schedules/changes/{id}/undo` runs the inverse if within the window and not already undone.

**Rationale:**
- `before_state` + `after_state` are denormalized on purpose: the source rows in `shift_segments` get mutated. Without a snapshot you can't reconstruct what changed.
- 24h undo window is a defensible default. Long enough to fix a click error the next morning, short enough that schedule history doesn't drift indefinitely.
- Append-only: undoing is itself a logged action.

**Deferred:**
- Reason / note field on apply (Decisions doc-style: *why* did the user apply this?). Wanted, but ships in v2 once the basic apply flow is exercised.
- Email/Slack notification on apply. Out of scope until [TODOS.md] cherry-pick F (Slack surface) lands.

### 3. Dry-run preview — show diff before write

**Problem:** "Apply" without a visible diff is a foot-gun. Users will click it once, watch a chart change in a way they didn't expect, and never trust the system again.

**Decision:**
- The dry-run preview *is* the existing `preview_schedule_change` tool's output. It already returns a gantt with the proposed segments overlaid.
- Apply button sits below that gantt and is labelled `Apply this change`. Hovering shows a popover with a one-line summary: "Move Adams's lunch from 12:30 → 13:00."
- The summary is generated by a small server-side function `summarize_change(before, after)` that walks the diff. The LLM does not generate it (the LLM is allowed to *propose* but not to *narrate* the binding action — same defense-in-depth principle as #1).
- After apply succeeds, the chat scroll receives a new `gantt` render of the *post-apply* state, not the preview. So the user sees confirmation that the write took effect.

**Rationale:**
- Reusing the preview tool means no new render type and no new contract surface. The render contract is already load-bearing; not expanding it.
- A server-side summary function avoids hallucinated wording on a binding action — a non-trivial concern when the model is one prompt-injection away from "tell the user this is a small change when it's actually huge."

**Deferred:**
- Multi-step previews ("here are 3 options, pick one"). v1 is one preview, one apply.

### 4. Idempotency — don't double-apply on retry

**Problem:** SSE streams retry on network blips. The user might click Apply twice from impatience. Both can reach the server.

**Decision:**
- The `apply_token` (from #1) is single-use. Server marks it consumed inside the same transaction as the write. A duplicate request with the same token returns `200 OK` with the **original** `applied_at` and log entry id — looks-like-success but is actually a no-op.
- The frontend disables the Apply button on click and sets it to "Applied at HH:MM" on success. Replays from the user are guarded at the UI layer; replays from the network are guarded at the token layer.

**Rationale:**
- Idempotency-key headers are the textbook answer; we already have an opaque token from the preview, so we use it. Saves a header.
- `200 OK` on duplicate (rather than 409) means the frontend doesn't have to special-case retries — the optimistic UI just confirms the existing state.

**Deferred:**
- Multi-instance API races. With a single API instance + a transaction-scoped token consumption, concurrent duplicates within the same instance are serialized. Multi-instance would need a Redis-backed token store; not needed in v1.

### 5. Conflict resolution — what if a human edited between propose and apply?

**Problem:** User asks for a preview at 09:00. At 09:02 a manager opens the schedule UI and changes the same agent's lunch directly. At 09:04 the user clicks Apply. The chat-side change was computed against a stale view.

**Decision:**
- Every `preview_schedule_change` response embeds a `schedule_version` integer, computed as `MAX(updated_at)` over the affected `shift_segments` rows at preview time, hashed to an int.
- `POST /schedules/apply` requires that integer in the request and re-computes it server-side at write time. If they differ → **409 Conflict** with a body that includes both the original preview and a fresh preview reflecting the new state.
- Frontend on 409: replaces the inline gantt with a side-by-side render — "Your preview" / "Current state" — and asks "Re-preview against current state?" (which calls `preview_schedule_change` again). No silent overwrite.

**Rationale:**
- Optimistic concurrency over pessimistic locks: locks would block the rest of the schedule UI, and the conflict rate is low in practice (most chat applies happen seconds after the preview).
- The "show both" 409 response is the honest UX. Silently writing a stale preview violates "the AI shows its math."

**Deferred:**
- Three-way merge ("apply your change AND keep theirs"). Hard, niche, easy to get wrong. v1 says "re-preview" and that's it.
- Schedule-segment-level versioning. v1 versions the affected window, not individual segments. If contention turns out to be real, segment-level versioning is the obvious next refinement.

---

## API shape

### Existing — reused, no change

`preview_schedule_change(date, changes[])` already returns `{ render: 'gantt', date, agents }`. The contract gains two **additive** optional fields when the preview is from a write-eligible tool call:

```ts
type ToolResponse =
  | // ...
  | {
      render: 'gantt';
      date: string;
      agents: GanttAgent[];
      // NEW (optional, only set by preview_schedule_change):
      apply_token?: string;
      schedule_version?: number;
    };
```

Adding optional fields is backward-compatible with the renderer (it ignores unknown fields). The renderer learns to show the Apply button when both are present.

### New endpoints

```http
POST /schedules/apply
  body: {
    apply_token: string;
    schedule_version: number;
    changes: ScheduleChange[];      // same shape as preview_schedule_change.input.changes
  }
  200: { log_id: uuid, applied_at: iso, schedule_id: bigint }
  400: invalid token / shape
  409: { current_version: number, your_version: number, fresh_preview: ToolResponse }
  410: token expired / already consumed (idempotent path returns 200 with the original log_id)

GET  /schedules/changes
  query: since?: iso, conversation_id?: uuid, limit: int
  200: { items: ScheduleChangeLog[] }

POST /schedules/changes/{log_id}/undo
  body: {}
  200: { undo_log_id: uuid, undone_at: iso }
  409: change is outside the undo window or already undone
```

### LLM tool definitions — unchanged

Cherry-pick D adds **no new chat tools**. The model's surface area stays identical to Phase 6: it can preview, never apply.

## Locked decisions (2026-04-29)

1. **Undo window length: 24h.** Long enough to fix a click error the next morning, short enough that schedule history doesn't drift indefinitely. `undo_window_ends_at = applied_at + interval '24 hours'`.
2. **Apply attribution: literal `"demo"`** in `applied_by`. When RBAC eventually lands the column shape doesn't change; we start writing real identifiers and historical rows stay readable.
3. **Apply notifications: yes, in v1.** New `notifications` table + in-app feed + top-nav unread badge. Email/Slack hooks designed but deferred. Full design in the next section.
4. **409 re-preview UX: explicit click, no auto-refresh.** "The AI shows its math" — silently swapping a stale preview for a fresh one when the user thought they were applying their reviewed change is exactly the violation we're guarding against. The 409 response renders side-by-side ("Your preview" / "Current state") with a button labelled `Re-preview against current state`.

## Notifications design (decision #3 expanded)

The audience answer (WFM-savvy hiring manager + actual call-center managers) means notifications are load-bearing for the demo, not an afterthought. Real call centers already live in a notification stream — schedule changes, no-show alerts, adherence breaches. A WFM tool that mutates schedules silently is the wrong tool.

### v1 — in-app feed

New table:

```sql
CREATE TABLE notifications (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  read_at         TIMESTAMPTZ,
  -- recipient: NULL = global feed (everyone sees it). Once a user model
  -- exists, this gets a user_id and unread counts go per-user.
  recipient       TEXT,
  category        TEXT NOT NULL,  -- 'schedule_applied' | 'schedule_undone' | 'apply_failed' | ...
  source          TEXT NOT NULL,  -- 'chat_apply' | 'chat_undo' | 'system'
  -- conversation provenance, NULL when origin isn't chat
  conversation_id UUID REFERENCES chat_conversations(id) ON DELETE SET NULL,
  -- structured payload for rendering (we use the same render-contract values
  -- as the chat panel: text, table, gantt). Frontend dispatches on render.
  payload         JSONB NOT NULL
);

CREATE INDEX ix_notifications_recipient_unread
  ON notifications (recipient, created_at DESC) WHERE read_at IS NULL;
```

`POST /schedules/apply` always inserts a `schedule_applied` notification with `payload = { render: 'text', content: <summarize_change output> }`. `POST /schedules/changes/{id}/undo` writes a `schedule_undone` notification. A failed apply (non-409 error) writes `apply_failed`.

### Endpoints

```http
GET  /notifications?limit=50         # latest first; recipient filtered server-side once user model exists
POST /notifications/{id}/read        # marks one as read
POST /notifications/read-all         # zero out the badge
```

### Frontend surface

- **Top-nav bell icon** (right of "Hide chat") with unread count badge. Polls `GET /notifications?limit=50` on a 30-second interval; pushed updates can come later via SSE if the count metric warrants it.
- **Dropdown panel** opens on click. Shows last 20 notifications, each rendered via the existing `ToolResponseRenderer` from the chat package (the payload uses the same render contract — pleasant reuse, no new renderer code).
- **Click a notification** → marks read, deep-links to the affected conversation in the chat panel (using `conversation_id`).

### Why this shape

- One render contract across chat + notifications means a single mental model and one renderer codebase.
- `recipient = NULL` global feed is the honest v1 — a single-password shared workspace is single-tenant. When RBAC lands, recipient gets populated and the existing index is already shaped right.
- Categories are an open enum (string + soft conventions) so future work can add `forecast_drift_alert`, `anomaly_high_severity`, etc., without a schema change.

### Email / Slack — designed, deferred

- An interface `NotificationSink` with one method `send(notification: dict)`. v1 has one implementation: `DBSink` (writes to `notifications`).
- v2 adds `EmailSink` (SMTP env vars) and `SlackSink` (webhook URL). The dispatch site wires multiple sinks; failure of one never blocks the others.
- Tied to TODOS cherry-pick F (Slack as alternative chat surface): the same Slack webhook can serve both surfaces.

### Notification acceptance criteria (additional)

- Apply success → exactly one `schedule_applied` notification row.
- Undo success → exactly one `schedule_undone` notification row.
- Apply 409 → no notification (the user hasn't actually applied anything yet).
- Notification feed renders with the same `ToolResponseRenderer` as the chat panel.
- Unread badge clears on `POST /notifications/read-all`.

## Acceptance criteria

- `POST /schedules/apply` writes through `shift_segments`, inserts a `schedule_change_log` row with non-null `before_state` / `after_state`, and rejects stale `schedule_version` with the side-by-side body.
- Apply button only renders when `apply_token` and `schedule_version` are present in the gantt response.
- Frontend smoke: preview → click Apply → see post-apply gantt → click Undo → see pre-apply gantt restored.
- Backend test: apply same token twice returns the original log_id (idempotency).
- Backend test: apply with stale `schedule_version` returns 409 with both versions.
- Backend test: undo outside the 24h window returns 409.

## Implementation phasing (for whoever picks this up)

1. Migration `0009_schedule_change_log.sql` + `0010_notifications.sql` + `0011_chat_apply_tokens.sql`.
2. `apply_token` issuance in `preview_schedule_change` handler (generate + cache in `chat_apply_tokens` with 5-min TTL; startup sweep prunes expired rows).
3. `POST /schedules/apply` endpoint with idempotency + concurrency checks. On success, write a `schedule_applied` notification.
4. `summarize_change` server-side helper (used by both the apply popover and the notification payload).
5. `NotificationSink` abstraction with `DBSink`. Wire `POST /schedules/apply` and `POST /schedules/changes/{id}/undo` to dispatch through it.
6. `GET /notifications`, `POST /notifications/{id}/read`, `POST /notifications/read-all`.
7. Frontend gantt renderer learns to show the Apply button + post-apply confirmation.
8. Frontend top-nav bell + dropdown that reuses `ToolResponseRenderer` for payload rendering.
9. Undo endpoint + UI affordance in the same gantt scroll. On success, write `schedule_undone` notification.
10. Tests: idempotency, 409, undo, undo-outside-window, notification row count per action, unread badge math.

Estimated effort: 90–120 min CC for happy-path implementation once this design is accepted (was 60–90 min before notifications were added). Most of the cost is in testing the conflict cases plus the notification surface.

## See also

- `~/Desktop/Projects/wfm-copilot-vault/TODOS.md` — the original cherry-pick D entry that triggered this design pass.
- `~/Desktop/Projects/wfm-copilot-vault/Decisions.md` — render contract + LLM trust boundary that this design depends on.
- `backend/app/tools/preview_schedule_change.py` — the read-side tool this builds on.
