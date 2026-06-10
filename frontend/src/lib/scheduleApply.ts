import type { ToolResponse } from "@/chat/types";
import { HAS_BACKEND, PROXY_BASE } from "@/lib/backendProxy";

export type ApplyChange = {
  agent_id: string;
  start: string;
  end: string;
  activity: string;
};

export type ApplyRequest = {
  apply_token: string;
  schedule_version: number;
  changes: ApplyChange[];
};

export type ApplyResponse = {
  log_id: string;
  applied_at: string;
  schedule_id: number;
};

export class ApplyError extends Error {
  constructor(
    message: string,
    public kind: "conflict" | "expired" | "not_found" | "transport" | "unknown",
    public freshPreview?: Extract<ToolResponse, { render: "gantt" }>,
  ) {
    super(message);
    this.name = "ApplyError";
  }
}

export async function applySchedule(req: ApplyRequest): Promise<ApplyResponse> {
  if (!HAS_BACKEND) {
    // Mock-provider mode: pretend we applied. Useful for local UI work.
    return {
      log_id: "mock-log-id",
      applied_at: new Date().toISOString(),
      schedule_id: 0,
    };
  }
  const res = await fetch(`${PROXY_BASE}/schedules/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (res.ok) return (await res.json()) as ApplyResponse;

  // Map known error shapes to typed ApplyError so the UI can branch cleanly.
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
    "fresh_preview" in detail
  ) {
    const d = detail as {
      fresh_preview: Extract<ToolResponse, { render: "gantt" }>;
    };
    throw new ApplyError(
      "The schedule changed since this preview.",
      "conflict",
      d.fresh_preview,
    );
  }
  if (res.status === 410) {
    throw new ApplyError("This preview is too old to apply (5-minute TTL).", "expired");
  }
  if (res.status === 404) {
    throw new ApplyError("Apply token was not found on the server.", "not_found");
  }
  throw new ApplyError(
    `Apply failed with status ${res.status}.`,
    "transport",
  );
}
