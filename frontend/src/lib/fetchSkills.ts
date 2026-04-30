/**
 * First real-data bridge for the dashboard.
 *
 * The frontend has been hardcoded to sales/support/billing since Phase 7.
 * This helper fetches the live skill list from `GET /skills` when an API
 * base is configured, with a hardcoded fallback for mock-provider mode (and
 * for transient backend errors — the picker should never fail the page).
 */
import {
  SKILLS,
  SKILL_LABEL,
  type SkillKey,
} from "./skills";

const API_BASE = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
const DEMO_PASSWORD = process.env.NEXT_PUBLIC_DEMO_PASSWORD;
const FETCH_TIMEOUT_MS = 1500;  // small — picker should never block layout

export type Skill = {
  id: number | null;
  name: string;
  description: string | null;
  primary_agent_count: number;
  secondary_agent_count: number;
};

/**
 * Hardcoded fallback. id=null distinguishes from server-fetched rows so
 * callers can tell the difference (e.g. avoid POST /forecasts {skill_id:1}
 * when the id is just a placeholder).
 */
export const FALLBACK_SKILLS: Skill[] = SKILLS.map((key: SkillKey) => ({
  id: null,
  name: key,
  description: SKILL_LABEL[key],
  primary_agent_count: 0,
  secondary_agent_count: 0,
}));

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = {};
  if (DEMO_PASSWORD) h.Authorization = "Basic " + btoa(`demo:${DEMO_PASSWORD}`);
  return h;
}

export async function fetchSkills(): Promise<Skill[]> {
  if (!API_BASE) return FALLBACK_SKILLS;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE}/skills`, {
      headers: authHeaders(),
      signal: controller.signal,
    });
    if (!res.ok) return FALLBACK_SKILLS;
    const data = (await res.json()) as Skill[];
    if (!Array.isArray(data) || data.length === 0) return FALLBACK_SKILLS;
    return data;
  } catch {
    // Network error, abort, parse error — fall back. The picker stays usable
    // even if the API is unreachable.
    return FALLBACK_SKILLS;
  } finally {
    clearTimeout(timer);
  }
}
