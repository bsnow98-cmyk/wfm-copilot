import { HAS_BACKEND, PROXY_BASE } from "@/lib/backendProxy";

export type VacationAwardResponse = {
  log_id: string;
  round_id: number;
  n_awarded: number;
  n_zero_win: number;
  applied_at: string;
};

export class VacationAwardError extends Error {
  constructor(
    message: string,
    public kind: "conflict" | "expired" | "not_found" | "forbidden" | "transport",
  ) {
    super(message);
    this.name = "VacationAwardError";
  }
}

export async function awardVacationBids(
  roundId: number,
  apply_token: string,
): Promise<VacationAwardResponse> {
  if (!HAS_BACKEND) {
    return {
      log_id: "mock-log-id",
      round_id: roundId,
      n_awarded: 0,
      n_zero_win: 0,
      applied_at: new Date().toISOString(),
    };
  }
  const res = await fetch(`${PROXY_BASE}/vacation/rounds/${roundId}/award`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ apply_token }),
  });
  if (res.ok) return (await res.json()) as VacationAwardResponse;
  if (res.status === 409)
    throw new VacationAwardError(
      "Bids or capacity changed since this preview — re-preview before awarding.",
      "conflict",
    );
  if (res.status === 410)
    throw new VacationAwardError("This preview is too old to award (5-minute TTL).", "expired");
  if (res.status === 404) throw new VacationAwardError("Apply token not found.", "not_found");
  if (res.status === 403)
    throw new VacationAwardError("Awarding vacation bids requires a manager.", "forbidden");
  throw new VacationAwardError(`Award failed with status ${res.status}.`, "transport");
}
