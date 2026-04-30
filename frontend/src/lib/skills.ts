/**
 * Phase 8 Stage 4 — multi-skill UI helpers.
 *
 * The skill list is hardcoded to match the synthetic-data generator's
 * SKILL_PROFILES (sales / support / billing). When the dashboard wires up
 * to real data, replace this with a fetch from a future `/skills` endpoint.
 *
 * Per-skill colors stay close to the design tokens — accent for the
 * "default" skill plus two muted derivatives so a multi-curve chart still
 * reads on a calm white surface. NO new accent introductions; we tint
 * within the existing palette.
 */

export type SkillKey = "sales" | "support" | "billing";

export const SKILLS: readonly SkillKey[] = ["sales", "support", "billing"] as const;

export const SKILL_LABEL: Record<SkillKey, string> = {
  sales: "Sales",
  support: "Support",
  billing: "Billing",
};

/** All-skills sentinel value for the picker. */
export const ALL_SKILLS = "__all__" as const;
export type SkillFilter = SkillKey | typeof ALL_SKILLS;

/**
 * Colors for multi-curve charts and skill badges. Accent (deep teal) is the
 * primary; the other two are darker neutrals so the eye reads "multiple
 * series, same calm palette." If we ever exceed 3 skills the cycle is
 * deliberate (not random).
 */
export const SKILL_COLOR: Record<SkillKey, string> = {
  sales: "#0F766E",       // accent
  support: "#525252",     // text-secondary
  billing: "#A3A3A3",     // border-strong
};

export function isSkillKey(value: string): value is SkillKey {
  return (SKILLS as readonly string[]).includes(value);
}
