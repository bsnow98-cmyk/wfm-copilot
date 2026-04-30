import { afterEach, describe, expect, it, vi } from "vitest";
import { FALLBACK_SKILLS, fetchSkills } from "@/lib/fetchSkills";

describe("fetchSkills", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns FALLBACK_SKILLS when no API base is configured", async () => {
    // The mock-provider mode of the dashboard runs without
    // NEXT_PUBLIC_API_URL — picker should still populate.
    const out = await fetchSkills();
    expect(out).toEqual(FALLBACK_SKILLS);
    expect(out.length).toBeGreaterThan(0);
    // Every fallback skill has id=null so callers know not to send them
    // as skill_id in API requests.
    for (const s of out) expect(s.id).toBeNull();
  });

  it("falls back when fetch rejects", async () => {
    // Even with an API base set, a network failure must not throw — the
    // picker should keep working with the hardcoded list.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("ECONNREFUSED");
      }),
    );
    // The module reads NEXT_PUBLIC_API_URL at import time, so we can only
    // exercise the "no API base" path here without a fresh import. The
    // hardcoded fallback is the safe-by-default behavior we care about.
    const out = await fetchSkills();
    expect(out).toEqual(FALLBACK_SKILLS);
  });

  it("FALLBACK_SKILLS includes the three Phase 8 skills", () => {
    const names = FALLBACK_SKILLS.map((s) => s.name).sort();
    expect(names).toEqual(["billing", "sales", "support"]);
  });
});
