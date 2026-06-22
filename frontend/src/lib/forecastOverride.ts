import { HAS_BACKEND, PROXY_BASE } from "@/lib/backendProxy";

export type ForecastOverrideApplyRequest = {
  apply_token: string;
};

export type ForecastOverrideApplyResponse = {
  log_id: string;
  forecast_run_id: number;
  interval_start: string;
  before_value: number;
  after_value: number;
  applied_at: string;
};

export class ForecastOverrideError extends Error {
  constructor(
    message: string,
    public kind: "conflict" | "expired" | "not_found" | "transport" | "unknown",
    public currentValue?: number | null,
  ) {
    super(message);
    this.name = "ForecastOverrideError";
  }
}

export async function applyForecastOverride(
  req: ForecastOverrideApplyRequest,
): Promise<ForecastOverrideApplyResponse> {
  if (!HAS_BACKEND) {
    return {
      log_id: "mock-log-id",
      forecast_run_id: 0,
      interval_start: new Date().toISOString(),
      before_value: 0,
      after_value: 0,
      applied_at: new Date().toISOString(),
    };
  }
  const res = await fetch(`${PROXY_BASE}/forecast/overrides/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (res.ok) return (await res.json()) as ForecastOverrideApplyResponse;

  let body: { detail?: unknown } = {};
  try {
    body = await res.json();
  } catch {
    /* opaque */
  }
  const detail = body.detail;
  if (
    res.status === 409 &&
    detail &&
    typeof detail === "object" &&
    "current_version" in detail
  ) {
    const d = detail as { current_value?: number | null };
    throw new ForecastOverrideError(
      "The forecast value changed since this preview.",
      "conflict",
      d.current_value,
    );
  }
  if (res.status === 410) {
    throw new ForecastOverrideError("This preview is too old to apply (5-minute TTL).", "expired");
  }
  if (res.status === 404) {
    throw new ForecastOverrideError("Apply token was not found on the server.", "not_found");
  }
  throw new ForecastOverrideError(`Apply failed with status ${res.status}.`, "transport");
}
