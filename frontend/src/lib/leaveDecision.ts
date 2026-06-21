import type { ToolResponse } from "@/chat/types";
import { HAS_BACKEND, PROXY_BASE } from "@/lib/backendProxy";

export type LeaveApplyRequest = {
  apply_token: string;
};

export type LeaveApplyResponse = {
  log_id: string;
  request_id: number;
  status: "approved" | "denied";
  decided_at: string;
};

export class LeaveApplyError extends Error {
  constructor(
    message: string,
    public kind: "conflict" | "expired" | "not_found" | "transport" | "unknown",
    public freshPreview?: Extract<ToolResponse, { render: "table" }>,
    public currentStatus?: string | null,
  ) {
    super(message);
    this.name = "LeaveApplyError";
  }
}

export async function applyLeaveDecision(
  req: LeaveApplyRequest,
): Promise<LeaveApplyResponse> {
  if (!HAS_BACKEND) {
    // Mock-provider mode: pretend we applied. Useful for local UI work.
    return {
      log_id: "mock-log-id",
      request_id: 0,
      status: "approved",
      decided_at: new Date().toISOString(),
    };
  }
  const res = await fetch(`${PROXY_BASE}/leave/decisions/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (res.ok) return (await res.json()) as LeaveApplyResponse;

  let body: { detail?: unknown } = {};
  try {
    body = await res.json();
  } catch {
    /* opaque error */
  }
  const detail = body.detail;

  if (
    res.status === 409 &&
    detail &&
    typeof detail === "object" &&
    "current_version" in detail
  ) {
    const d = detail as {
      fresh_preview?: Extract<ToolResponse, { render: "table" }>;
      current_status?: string | null;
    };
    throw new LeaveApplyError(
      "This request was already decided since the preview.",
      "conflict",
      d.fresh_preview,
      d.current_status,
    );
  }
  if (res.status === 410) {
    throw new LeaveApplyError("This preview is too old to apply (5-minute TTL).", "expired");
  }
  if (res.status === 404) {
    throw new LeaveApplyError("Apply token was not found on the server.", "not_found");
  }
  throw new LeaveApplyError(`Apply failed with status ${res.status}.`, "transport");
}
