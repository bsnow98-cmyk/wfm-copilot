import { HAS_BACKEND, PROXY_BASE } from "@/lib/backendProxy";

export type RecomputeStatus = "pending" | "running" | "completed" | "failed";

export type StaffingTargetApplyResponse = {
  log_id: string;
  staffing_id: number;
  recompute_status: RecomputeStatus;
  peak_required_before: number;
  before_targets: Record<string, unknown>;
  after_targets: Record<string, unknown>;
  applied_at: string;
};

export type StaffingTargetStatus = StaffingTargetApplyResponse & {
  recompute_error?: string | null;
  peak_required_after?: number | null;
  completed_at?: string | null;
  undone_at?: string | null;
};

export class StaffingTargetError extends Error {
  constructor(
    message: string,
    public kind: "conflict" | "expired" | "not_found" | "transport" | "unknown",
  ) {
    super(message);
    this.name = "StaffingTargetError";
  }
}

export async function applyStaffingTarget(
  apply_token: string,
): Promise<StaffingTargetApplyResponse> {
  if (!HAS_BACKEND) {
    return {
      log_id: "mock-log-id",
      staffing_id: 0,
      recompute_status: "completed",
      peak_required_before: 38,
      before_targets: {},
      after_targets: {},
      applied_at: new Date().toISOString(),
    };
  }
  const res = await fetch(`${PROXY_BASE}/staffing/targets/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ apply_token }),
  });
  if (res.ok) return (await res.json()) as StaffingTargetApplyResponse;
  if (res.status === 409) throw new StaffingTargetError("Targets changed since this preview.", "conflict");
  if (res.status === 410) throw new StaffingTargetError("This preview is too old to apply (5-minute TTL).", "expired");
  if (res.status === 404) throw new StaffingTargetError("Apply token was not found on the server.", "not_found");
  throw new StaffingTargetError(`Apply failed with status ${res.status}.`, "transport");
}

export async function getStaffingStatus(logId: string): Promise<StaffingTargetStatus> {
  const res = await fetch(`${PROXY_BASE}/staffing/targets/${logId}`);
  if (!res.ok) throw new StaffingTargetError(`Status check failed (${res.status}).`, "transport");
  return (await res.json()) as StaffingTargetStatus;
}

/**
 * Poll the recompute status until it leaves pending/running (or the budget runs
 * out). The recompute is sub-second in practice, but polling keeps the UI honest
 * about the async job. Mock mode short-circuits to a completed status.
 */
export async function pollStaffingStatus(
  logId: string,
  { intervalMs = 1200, maxAttempts = 16 } = {},
): Promise<StaffingTargetStatus> {
  if (!HAS_BACKEND) {
    return {
      log_id: logId,
      staffing_id: 0,
      recompute_status: "completed",
      peak_required_before: 38,
      peak_required_after: 39,
      before_targets: {},
      after_targets: {},
      applied_at: new Date().toISOString(),
    };
  }
  let last: StaffingTargetStatus | null = null;
  for (let i = 0; i < maxAttempts; i++) {
    last = await getStaffingStatus(logId);
    if (last.recompute_status === "completed" || last.recompute_status === "failed") {
      return last;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return last as StaffingTargetStatus;
}
