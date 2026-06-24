"use client";

import { useState } from "react";
import type {
  ForecastOverridePreview,
  LeaveDecisionPreview,
  OfferPreview,
  StaffingTargetPreview,
  ToolResponse,
  VacationAwardPreview,
} from "../types";
import { applyLeaveDecision, LeaveApplyError } from "@/lib/leaveDecision";
import { publishOffer, OfferApplyError } from "@/lib/offerApply";
import { applyForecastOverride, ForecastOverrideError } from "@/lib/forecastOverride";
import { applyStaffingTarget, pollStaffingStatus } from "@/lib/staffingTarget";
import { awardVacationBids, VacationAwardError } from "@/lib/vacationAward";

export function TableRenderer({
  response,
}: {
  response: Extract<ToolResponse, { render: "table" }>;
}) {
  return (
    <figure className="border border-border-default rounded-md overflow-hidden">
      {response.title ? (
        <figcaption className="text-sm text-text-primary px-4 py-3 border-b border-border-default">
          {response.title}
        </figcaption>
      ) : null}
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-label={response.title ?? "Table"}>
          <thead>
            <tr className="bg-surface-subtle text-left text-text-secondary">
              {response.columns.map((c) => (
                <th
                  key={c}
                  className="font-medium px-4 py-2 border-b border-border-default"
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {response.rows.map((row, ri) => (
              <tr
                key={ri}
                className={ri % 2 === 1 ? "bg-surface-subtle" : "bg-surface"}
              >
                {row.map((cell, ci) => (
                  <td
                    key={ci}
                    className="px-4 py-2 align-top"
                    data-mono={typeof cell === "number" ? "" : undefined}
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {response.apply_token && response.leave_decision ? (
        <LeaveDecisionAffordance
          applyToken={response.apply_token}
          decision={response.leave_decision}
        />
      ) : null}
      {response.apply_token && response.offer ? (
        <OfferAffordance applyToken={response.apply_token} offer={response.offer} />
      ) : null}
      {response.apply_token && response.forecast_override ? (
        <ForecastOverrideAffordance
          applyToken={response.apply_token}
          override={response.forecast_override}
        />
      ) : null}
      {response.apply_token && response.staffing_target ? (
        <StaffingTargetAffordance
          applyToken={response.apply_token}
          target={response.staffing_target}
        />
      ) : null}
      {response.apply_token && response.vacation_award ? (
        <VacationAwardAffordance
          applyToken={response.apply_token}
          award={response.vacation_award}
        />
      ) : null}
    </figure>
  );
}

function VacationAwardAffordance({
  applyToken,
  award,
}: {
  applyToken: string;
  award: VacationAwardPreview;
}) {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "awarding" }
    | { kind: "awarded"; nAwarded: number }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  async function handleAward() {
    setState({ kind: "awarding" });
    try {
      const out = await awardVacationBids(award.round_id, applyToken);
      setState({ kind: "awarded", nAwarded: out.n_awarded });
    } catch (err) {
      const message =
        err instanceof VacationAwardError || err instanceof Error
          ? err.message
          : "Award failed for an unknown reason.";
      setState({ kind: "error", message });
    }
  }

  if (state.kind === "awarded") {
    return (
      <div className="px-4 py-2 border-t border-border-default text-xs text-text-muted">
        Awarded <span data-mono>{state.nAwarded}</span> weeks. Committed silently —
        review, then publish to notify agents. Undoable for 24 hours.
      </div>
    );
  }

  return (
    <div className="px-4 py-2 border-t border-border-default flex items-center gap-3">
      <button
        type="button"
        onClick={handleAward}
        disabled={state.kind === "awarding"}
        className="text-sm px-3 py-1.5 rounded-sm bg-accent text-white disabled:bg-border-strong"
        title={`Award ${award.n_awarded} weeks to ${award.n_agents} agents`}
      >
        {state.kind === "awarding"
          ? "Awarding…"
          : `Award ${award.n_awarded} weeks to ${award.n_agents} agent${award.n_agents === 1 ? "" : "s"}`}
      </button>
      {state.kind === "error" ? (
        <span className="text-xs text-severity-high">{state.message}</span>
      ) : (
        <span className="text-xs text-text-muted">
          {award.n_zero_win} get nothing · {award.weeks_at_capacity} weeks maxed. Commits
          silently (publish notifies separately); undoable 24h. Manager only.
        </span>
      )}
    </div>
  );
}

function StaffingTargetAffordance({
  applyToken,
  target,
}: {
  applyToken: string;
  target: StaffingTargetPreview;
}) {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "applying" }
    | { kind: "recomputing" }
    | { kind: "done"; peakAfter: number | null }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  async function handleApply() {
    setState({ kind: "applying" });
    try {
      const out = await applyStaffingTarget(applyToken);
      setState({ kind: "recomputing" });
      const final = await pollStaffingStatus(out.log_id);
      if (final.recompute_status === "failed") {
        setState({ kind: "error", message: final.recompute_error || "Recompute failed." });
        return;
      }
      setState({ kind: "done", peakAfter: final.peak_required_after ?? null });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Apply failed for an unknown reason.";
      setState({ kind: "error", message });
    }
  }

  if (state.kind === "done") {
    return (
      <div className="px-4 py-2 border-t border-border-default text-xs text-text-muted">
        Targets applied & staffing recomputed — peak required{" "}
        <span data-mono>{target.peak_before}</span> →{" "}
        <span data-mono>{state.peakAfter ?? target.peak_after_est}</span>. Undoable for 24 hours.
      </div>
    );
  }

  return (
    <div className="px-4 py-2 border-t border-border-default flex items-center gap-3">
      <button
        type="button"
        onClick={handleApply}
        disabled={state.kind === "applying" || state.kind === "recomputing"}
        className="text-sm px-3 py-1.5 rounded-sm bg-accent text-white disabled:bg-border-strong"
        title={`Apply new targets for ${target.queue} and recompute staffing`}
      >
        {state.kind === "applying"
          ? "Applying…"
          : state.kind === "recomputing"
            ? "Recomputing staffing…"
            : "Apply targets & recompute"}
      </button>
      {state.kind === "error" ? (
        <span className="text-xs text-severity-high">{state.message}</span>
      ) : (
        <span className="text-xs text-text-muted">
          Recompute runs as a background job. Peak required ~{target.peak_before} →{" "}
          {target.peak_after_est}. Undoable for 24 hours.
        </span>
      )}
    </div>
  );
}

function ForecastOverrideAffordance({
  applyToken,
  override,
}: {
  applyToken: string;
  override: ForecastOverridePreview;
}) {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "applying" }
    | { kind: "applied"; appliedAt: string }
    | { kind: "conflict"; current?: number | null }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  async function handleApply() {
    setState({ kind: "applying" });
    try {
      const out = await applyForecastOverride({ apply_token: applyToken });
      setState({ kind: "applied", appliedAt: out.applied_at });
    } catch (err) {
      if (err instanceof ForecastOverrideError && err.kind === "conflict") {
        setState({ kind: "conflict", current: err.currentValue });
        return;
      }
      const message =
        err instanceof Error ? err.message : "Apply failed for an unknown reason.";
      setState({ kind: "error", message });
    }
  }

  if (state.kind === "applied") {
    return (
      <div className="px-4 py-2 border-t border-border-default text-xs text-text-muted">
        Override applied <span data-mono>{state.appliedAt.slice(11, 16)}</span>. Undoable for 24
        hours. Recompute staffing to propagate.
      </div>
    );
  }

  if (state.kind === "conflict") {
    return (
      <div className="px-4 py-2 border-t border-border-default text-xs text-severity-medium">
        The forecast value changed since this preview
        {state.current != null ? ` (now ${state.current})` : ""}. Re-preview before applying.
      </div>
    );
  }

  return (
    <div className="px-4 py-2 border-t border-border-default flex items-center gap-3">
      <button
        type="button"
        onClick={handleApply}
        disabled={state.kind === "applying"}
        className="text-sm px-3 py-1.5 rounded-sm bg-accent text-white disabled:bg-border-strong"
        title={`Pin ${override.interval_label} to ${override.proposed}`}
      >
        {state.kind === "applying"
          ? "Applying…"
          : `Override to ${Math.round(override.proposed)}`}
      </button>
      {state.kind === "error" ? (
        <span className="text-xs text-severity-high">{state.message}</span>
      ) : (
        <span className="text-xs text-text-muted">
          Pins {override.queue} {override.interval_label}. Undoable for 24 hours; staffing
          recompute is separate.
        </span>
      )}
    </div>
  );
}

function OfferAffordance({
  applyToken,
  offer,
}: {
  applyToken: string;
  offer: OfferPreview;
}) {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "publishing" }
    | { kind: "published"; offerId: number; publishedAt: string }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  const label = offer.kind.toUpperCase();

  async function handlePublish() {
    setState({ kind: "publishing" });
    try {
      const out = await publishOffer({ apply_token: applyToken });
      setState({ kind: "published", offerId: out.offer_id, publishedAt: out.published_at });
    } catch (err) {
      const message =
        err instanceof OfferApplyError || err instanceof Error
          ? err.message
          : "Publish failed for an unknown reason.";
      setState({ kind: "error", message });
    }
  }

  if (state.kind === "published") {
    return (
      <div className="px-4 py-2 border-t border-border-default text-xs text-text-muted">
        {label} offer published{" "}
        <span data-mono>#{state.offerId}</span> at{" "}
        <span data-mono>{state.publishedAt.slice(11, 16)}</span>. Retractable for 24 hours.
      </div>
    );
  }

  return (
    <div className="px-4 py-2 border-t border-border-default flex items-center gap-3">
      <button
        type="button"
        onClick={handlePublish}
        disabled={state.kind === "publishing"}
        className="text-sm px-3 py-1.5 rounded-sm bg-accent text-white disabled:bg-border-strong"
        title={`Publish this ${label} offer to ${offer.n_targets} agents`}
      >
        {state.kind === "publishing"
          ? "Publishing…"
          : `Publish ${label} offer to ${offer.n_targets} agent${offer.n_targets === 1 ? "" : "s"}`}
      </button>
      {state.kind === "error" ? (
        <span className="text-xs text-severity-high">{state.message}</span>
      ) : (
        <span className="text-xs text-text-muted">
          {offer.window_label} · {offer.slots} slot{offer.slots === 1 ? "" : "s"}. Retractable for 24
          hours.
        </span>
      )}
    </div>
  );
}

function LeaveDecisionAffordance({
  applyToken,
  decision,
}: {
  applyToken: string;
  decision: LeaveDecisionPreview;
}) {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "applying" }
    | { kind: "applied"; status: string; decidedAt: string }
    | { kind: "conflict"; status?: string | null }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  const isApprove = decision.decision === "approve";
  const verb = isApprove ? "Approve" : "Deny";

  async function handleApply() {
    setState({ kind: "applying" });
    try {
      const out = await applyLeaveDecision({ apply_token: applyToken });
      setState({ kind: "applied", status: out.status, decidedAt: out.decided_at });
    } catch (err) {
      if (err instanceof LeaveApplyError && err.kind === "conflict") {
        setState({ kind: "conflict", status: err.currentStatus });
        return;
      }
      const message =
        err instanceof Error ? err.message : "Apply failed for an unknown reason.";
      setState({ kind: "error", message });
    }
  }

  if (state.kind === "applied") {
    return (
      <div className="px-4 py-2 border-t border-border-default text-xs text-text-muted">
        {state.status === "approved" ? "Approved" : "Denied"}{" "}
        <span data-mono>{state.decidedAt.slice(11, 16)}</span>. Undoable for 24 hours.
      </div>
    );
  }

  if (state.kind === "conflict") {
    return (
      <div className="px-4 py-2 border-t border-border-default text-xs text-severity-medium">
        This request was already decided
        {state.status ? ` (now ${state.status})` : ""} since the preview. Re-run the
        recommendation to see the current state.
      </div>
    );
  }

  return (
    <div className="px-4 py-2 border-t border-border-default flex items-center gap-3">
      <button
        type="button"
        onClick={handleApply}
        disabled={state.kind === "applying"}
        className={
          isApprove
            ? "text-sm px-3 py-1.5 rounded-sm bg-accent text-white disabled:bg-border-strong"
            : "text-sm px-3 py-1.5 rounded-sm border border-border-strong text-text-primary disabled:text-text-muted"
        }
        title={`${verb} leave for ${decision.label}`}
      >
        {state.kind === "applying"
          ? `${verb}ing…`
          : `${verb} ${decision.label}`}
      </button>
      {state.kind === "error" ? (
        <span className="text-xs text-severity-high">{state.message}</span>
      ) : (
        <span className="text-xs text-text-muted">
          {isApprove && decision.pto_hours
            ? `Charges ${decision.pto_hours}h PTO. `
            : ""}
          A decision will be saved and undoable for 24 hours.
        </span>
      )}
    </div>
  );
}
