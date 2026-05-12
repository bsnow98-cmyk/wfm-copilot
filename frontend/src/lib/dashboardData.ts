/**
 * Dashboard data layer.
 *
 * Each `fetchXxx` returns a `ToolResponse | null` so the page components can
 * just hand it to `<ToolResponseRenderer />`. Returns `null` when:
 *   - `NEXT_PUBLIC_API_URL` is unset (local dev without a backend) — page
 *     falls back to its useMemo mock data and the visuals stay consistent.
 *   - The backend errors, times out, or returns empty rows — same fallback.
 *
 * No throws. Page-level rendering should never break because the API blinked.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
const DEMO_PASSWORD = process.env.NEXT_PUBLIC_DEMO_PASSWORD;
const FETCH_TIMEOUT_MS = 4500;

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = {};
  if (DEMO_PASSWORD) h.Authorization = "Basic " + btoa(`demo:${DEMO_PASSWORD}`);
  return h;
}

export const HAS_API: boolean = Boolean(API_BASE);

async function getJSON<T>(path: string): Promise<T | null> {
  if (!API_BASE) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: authHeaders(),
      signal: controller.signal,
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// ---------- types matching backend schemas ----------

type ForecastRun = {
  id: number;
  queue: string;
  channel: string;
  status: string;
  mape: number | null;
  wape: number | null;
};

type ForecastInterval = {
  interval_start: string;
  forecast_offered: number;
  forecast_aht_seconds: number;
};

type ScheduleSummary = {
  id: number;
  name: string;
  staffing_id: number;
  start_date: string;
  end_date: string;
  status: string;
};

type ShiftSegment = {
  agent_id: number;
  employee_id: string;
  full_name: string;
  segment_type:
    | "available"
    | "break"
    | "lunch"
    | "training"
    | "meeting"
    | "shrinkage"
    | "off";
  start_time: string;
  end_time: string;
};

type CoverageRow = {
  interval_start: string;
  required_agents: number;
  scheduled_agents: number;
  shortage: number;
};

type ScheduleDetail = ScheduleSummary & {
  shift_segments: ShiftSegment[];
  coverage: CoverageRow[];
};

type StaffingSummary = {
  id: number;
  forecast_run_id: number;
  service_level_target: number;
  target_answer_seconds: number;
  peak_required_agents: number;
};

type StaffingInterval = {
  interval_start: string;
  required_agents: number;
  expected_service_level: number;
  expected_asa_seconds: number;
};

type IntradayPoint = {
  interval_start: string;
  forecast: number | null;
  actual: number | null;
};

type IntradayToday = {
  queue: string;
  sim_now: string;
  points: IntradayPoint[];
};

// ---------- public fetchers ----------

export type ForecastDashboardData = {
  runId: number;
  queue: string;
  mape: number | null;
  wape: number | null;
  intervals: ForecastInterval[];
};

export async function fetchLatestForecast(): Promise<ForecastDashboardData | null> {
  const runs = await getJSON<ForecastRun[]>("/forecasts?limit=10");
  if (!runs || runs.length === 0) return null;
  const run = runs.find((r) => r.status === "completed") ?? runs[0];
  const intervals = await getJSON<ForecastInterval[]>(
    `/forecasts/${run.id}/intervals`,
  );
  if (!intervals || intervals.length === 0) return null;
  return {
    runId: run.id,
    queue: run.queue,
    mape: run.mape,
    wape: run.wape,
    intervals,
  };
}

export type ScheduleDashboardData = {
  scheduleId: number;
  name: string;
  startDate: string;
  endDate: string;
  segments: ShiftSegment[];
  coverage: CoverageRow[];
};

export async function fetchLatestSchedule(): Promise<ScheduleDashboardData | null> {
  const list = await getJSON<ScheduleSummary[]>("/schedules?limit=10");
  if (!list || list.length === 0) return null;
  const sched =
    list.find((s) => s.status === "published" || s.status === "completed") ??
    list[0];
  const detail = await getJSON<ScheduleDetail>(`/schedules/${sched.id}`);
  if (!detail) return null;
  return {
    scheduleId: detail.id,
    name: detail.name,
    startDate: detail.start_date,
    endDate: detail.end_date,
    segments: detail.shift_segments,
    coverage: detail.coverage,
  };
}

export type IntradayDashboardData = {
  queue: string;
  simNow: string;
  points: IntradayPoint[];
};

export async function fetchIntradayToday(
  queue = "auto",
): Promise<IntradayDashboardData | null> {
  const data = await getJSON<IntradayToday>(
    `/intraday/today?queue=${encodeURIComponent(queue)}`,
  );
  if (!data || data.points.length === 0) return null;
  return { queue: data.queue, simNow: data.sim_now, points: data.points };
}

export type StaffingDashboardData = {
  staffingId: number;
  serviceLevelTarget: number;
  targetAnswerSeconds: number;
  intervals: StaffingInterval[];
};

export async function fetchLatestStaffing(): Promise<StaffingDashboardData | null> {
  const list = await getJSON<StaffingSummary[]>("/staffing-requirements?limit=10");
  if (!list || list.length === 0) return null;
  const top = list[0];
  const intervals = await getJSON<StaffingInterval[]>(
    `/staffing-requirements/${top.id}/intervals`,
  );
  if (!intervals || intervals.length === 0) return null;
  return {
    staffingId: top.id,
    serviceLevelTarget: top.service_level_target,
    targetAnswerSeconds: top.target_answer_seconds,
    intervals,
  };
}
