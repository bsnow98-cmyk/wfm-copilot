import { HAS_BACKEND, PROXY_BASE } from "@/lib/backendProxy";

export type OfferApplyRequest = {
  apply_token: string;
};

export type OfferApplyResponse = {
  offer_id: number;
  kind: "ot" | "vto";
  slots: number;
  n_targets: number;
  published_at: string;
};

export class OfferApplyError extends Error {
  constructor(
    message: string,
    public kind: "expired" | "not_found" | "transport" | "unknown",
  ) {
    super(message);
    this.name = "OfferApplyError";
  }
}

export async function publishOffer(
  req: OfferApplyRequest,
): Promise<OfferApplyResponse> {
  if (!HAS_BACKEND) {
    return {
      offer_id: 0,
      kind: "ot",
      slots: 0,
      n_targets: 0,
      published_at: new Date().toISOString(),
    };
  }
  const res = await fetch(`${PROXY_BASE}/offers/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (res.ok) return (await res.json()) as OfferApplyResponse;

  if (res.status === 410) {
    throw new OfferApplyError("This preview is too old to publish (5-minute TTL).", "expired");
  }
  if (res.status === 404) {
    throw new OfferApplyError("Apply token was not found on the server.", "not_found");
  }
  throw new OfferApplyError(`Publish failed with status ${res.status}.`, "transport");
}
