"use client";

import { useEffect, useMemo, useState } from "react";
import { ToolResponseRenderer } from "@/chat/renderers";
import { useSkill } from "@/context/SkillContext";
import { ALL_SKILLS, SKILLS, SKILL_LABEL, type SkillKey } from "@/lib/skills";
import { ViewHeader } from "@/components/ViewHeader";
import type { ToolResponse } from "@/chat/types";
import { fetchLatestForecast, type ForecastDashboardData } from "@/lib/dashboardData";

const SKILL_WEIGHT: Record<SkillKey, number> = {
  sales: 0.30,
  support: 0.55,
  billing: 0.15,
};

type Granularity = "monthly" | "weekly" | "daily" | "hourly";

const GRANULARITIES: { key: Granularity; label: string }[] = [
  { key: "monthly", label: "Monthly" },
  { key: "weekly", label: "Weekly" },
  { key: "daily", label: "Daily" },
  { key: "hourly", label: "Hourly" },
];

const RANGE_LABEL: Record<Granularity, string> = {
  monthly: "next 12 months",
  weekly: "next 52 weeks",
  daily: "next 365 days",
  hourly: "next 7 days (hourly)",
};

function dayIntensity(i: number): number {
  return (
    1200 +
    Math.sin((i / 7) * 2 * Math.PI) * 220 +
    Math.sin((i / 365) * 2 * Math.PI + 1) * 180 +
    Math.sin(i / 11) * 60
  );
}

function hourIntensity(dayIdx: number, hour: number): number {
  const day = dayIntensity(dayIdx);
  // Intraday M-shape: low overnight, peaks ~10am and ~2pm.
  const open = hour >= 7 && hour <= 19;
  const t = (hour - 7) / 12;
  const shape = open
    ? Math.max(0, Math.sin(t * Math.PI) + 0.35 * Math.sin(t * Math.PI * 2))
    : 0;
  return (day / 24) * (0.25 + shape * 1.4);
}

function todayUTC(): Date {
  const d = new Date();
  d.setUTCHours(0, 0, 0, 0);
  return d;
}

function generateBasePoints(g: Granularity): { x: string; y: number }[] {
  const today = todayUTC();

  if (g === "monthly") {
    // First bucket = the calendar month containing today; sum the full month
    // (dayIntensity handles negative offsets for days earlier than today).
    return Array.from({ length: 12 }, (_, m) => {
      const monthDate = new Date(
        Date.UTC(today.getUTCFullYear(), today.getUTCMonth() + m, 1),
      );
      const ym = `${monthDate.getUTCFullYear()}-${String(monthDate.getUTCMonth() + 1).padStart(2, "0")}`;
      const monthStart = monthDate.getTime();
      const monthEnd = Date.UTC(
        monthDate.getUTCFullYear(),
        monthDate.getUTCMonth() + 1,
        1,
      );
      const days = Math.round((monthEnd - monthStart) / 86400000);
      const startOffset = Math.round((monthStart - today.getTime()) / 86400000);
      let sum = 0;
      for (let j = 0; j < days; j++) sum += dayIntensity(startOffset + j);
      return { x: ym, y: Math.round(sum) };
    });
  }

  if (g === "weekly") {
    // First bucket = the ISO week (Mon-Sun) containing today.
    const dowOffset = (today.getUTCDay() + 6) % 7; // 0 = Mon, 6 = Sun
    const weekStart = new Date(today);
    weekStart.setUTCDate(today.getUTCDate() - dowOffset);
    return Array.from({ length: 52 }, (_, w) => {
      const d = new Date(weekStart);
      d.setUTCDate(d.getUTCDate() + w * 7);
      let sum = 0;
      for (let j = 0; j < 7; j++) sum += dayIntensity(w * 7 + j - dowOffset);
      return { x: d.toISOString().slice(0, 10), y: Math.round(sum) };
    });
  }

  if (g === "daily") {
    return Array.from({ length: 365 }, (_, i) => {
      const d = new Date(today);
      d.setUTCDate(d.getUTCDate() + i);
      return { x: d.toISOString().slice(0, 10), y: Math.round(dayIntensity(i)) };
    });
  }

  // hourly: 7 days × 24h = 168 points
  return Array.from({ length: 7 * 24 }, (_, i) => {
    const dayIdx = Math.floor(i / 24);
    const hour = i % 24;
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() + dayIdx);
    const x = `${d.toISOString().slice(0, 10)}T${String(hour).padStart(2, "0")}:00`;
    return { x, y: Math.round(hourIntensity(dayIdx, hour)) };
  });
}

function multiCurveResponse(g: Granularity): Extract<ToolResponse, { render: "chart.line" }> {
  const base = generateBasePoints(g);
  const series = SKILLS.map((s) => ({
    name: SKILL_LABEL[s],
    points: base.map((p) => ({ x: p.x, y: Math.round(p.y * SKILL_WEIGHT[s]) })),
  }));
  return {
    render: "chart.line",
    title: `Forecast by skill — sales_inbound, ${RANGE_LABEL[g]}`,
    yLabel: "calls",
    series,
  };
}

function singleSkillResponse(
  skill: SkillKey,
  g: Granularity,
): Extract<ToolResponse, { render: "chart.line" }> {
  const base = generateBasePoints(g);
  const w = SKILL_WEIGHT[skill];
  return {
    render: "chart.line",
    title: `Forecast — ${SKILL_LABEL[skill]}, ${RANGE_LABEL[g]}`,
    yLabel: "calls",
    series: [
      {
        name: "Forecast",
        points: base.map((p) => ({ x: p.x, y: Math.round(p.y * w) })),
      },
    ],
  };
}

const DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function dayOfWeekResponse(
  skillFilter: SkillKey | typeof ALL_SKILLS,
): Extract<ToolResponse, { render: "chart.bar" }> {
  const today = todayUTC();
  const dowOffset = (today.getUTCDay() + 6) % 7; // 0 = Mon
  const weeks = 4;
  const sums = [0, 0, 0, 0, 0, 0, 0];
  const counts = [0, 0, 0, 0, 0, 0, 0];
  for (let i = 0; i < weeks * 7; i++) {
    const dow = ((dowOffset + i) % 7 + 7) % 7;
    sums[dow] += dayIntensity(i);
    counts[dow] += 1;
  }
  const w = skillFilter === ALL_SKILLS ? 1 : SKILL_WEIGHT[skillFilter];
  const skillSuffix = skillFilter === ALL_SKILLS ? "all skills" : SKILL_LABEL[skillFilter];
  return {
    render: "chart.bar",
    title: `Average calls by day of week — next ${weeks} weeks · ${skillSuffix}`,
    bars: DOW_LABELS.map((label, i) => ({
      label,
      value: Math.round((sums[i] / counts[i]) * w),
    })),
  };
}

function GranularityToggle({
  value,
  onChange,
}: {
  value: Granularity;
  onChange: (g: Granularity) => void;
}) {
  return (
    <div
      role="radiogroup"
      aria-label="Forecast granularity"
      className="inline-flex border border-border-default rounded-sm overflow-hidden"
    >
      {GRANULARITIES.map((g, i) => {
        const active = value === g.key;
        return (
          <button
            key={g.key}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(g.key)}
            className={
              "px-3 py-1.5 text-sm transition-colors " +
              (i > 0 ? "border-l border-border-default " : "") +
              (active
                ? "bg-accent/10 text-accent"
                : "text-text-secondary hover:text-text-primary")
            }
          >
            {g.label}
          </button>
        );
      })}
    </div>
  );
}

// Bucket live half-hour forecast intervals into the active granularity. Falls
// back to the synthetic generator when API_BASE is unset or returns no data —
// the visual stays consistent and the page never breaks on a backend blink.
function buildLiveResponse(
  live: ForecastDashboardData,
  g: Granularity,
  skill: SkillKey | typeof ALL_SKILLS,
  skillFiltered: boolean,
): Extract<ToolResponse, { render: "chart.line" }> {
  // SKILL_WEIGHT is the legacy approximation for scaling an AGGREGATE run
  // down to one skill. When the run was fetched skill-filtered server-side,
  // the data is already that skill's — no scaling.
  const w = skill === ALL_SKILLS || skillFiltered ? 1 : SKILL_WEIGHT[skill];
  const buckets = new Map<string, number>();
  for (const iv of live.intervals) {
    const d = new Date(iv.interval_start);
    let key: string;
    if (g === "monthly") {
      key = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
    } else if (g === "weekly") {
      const dow = (d.getUTCDay() + 6) % 7; // Mon=0
      const wkStart = new Date(d);
      wkStart.setUTCDate(d.getUTCDate() - dow);
      key = wkStart.toISOString().slice(0, 10);
    } else if (g === "daily") {
      key = d.toISOString().slice(0, 10);
    } else {
      const h = String(d.getUTCHours()).padStart(2, "0");
      key = `${d.toISOString().slice(0, 10)}T${h}:00`;
    }
    buckets.set(key, (buckets.get(key) ?? 0) + iv.forecast_offered);
  }
  const points = Array.from(buckets.entries())
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([x, y]) => ({ x, y: Math.round(y * w) }));
  const skillLabel =
    skill === ALL_SKILLS ? `queue ${live.queue}` : SKILL_LABEL[skill];
  return {
    render: "chart.line",
    title: `Forecast — ${skillLabel}, ${RANGE_LABEL[g]}`,
    yLabel: "calls",
    series: [{ name: "Forecast", points }],
  };
}

export default function ForecastPage() {
  const { skill, skills, skillsLoading } = useSkill();
  const [granularity, setGranularity] = useState<Granularity>("daily");
  const [live, setLive] = useState<ForecastDashboardData | null>(null);

  // Resolve the selected skill key to its server id (null for ALL_SKILLS or
  // when running on fallback skills, whose ids are placeholders).
  const skillId =
    skill === ALL_SKILLS
      ? null
      : skills.find((s) => s.name === skill)?.id ?? null;

  useEffect(() => {
    // Wait for the live skill list so a skill-filtered page doesn't first
    // fetch unfiltered and flash the wrong skill's data.
    if (skillsLoading) return;
    let cancelled = false;
    // Drop the previous skill's data immediately — a brief synthetic
    // fallback beats rendering stale data under the new skill's label.
    setLive(null);
    fetchLatestForecast(skillId).then((d) => {
      if (!cancelled) setLive(d);
    });
    return () => {
      cancelled = true;
    };
  }, [skillId, skillsLoading]);

  const response = useMemo(() => {
    if (live) return buildLiveResponse(live, granularity, skill, skillId != null);
    return skill === ALL_SKILLS
      ? multiCurveResponse(granularity)
      : singleSkillResponse(skill, granularity);
  }, [skill, granularity, live, skillId]);
  const dowChart = useMemo(() => dayOfWeekResponse(skill), [skill]);

  const skillSubtitle =
    skill === ALL_SKILLS ? "" : `Filtered to ${SKILL_LABEL[skill]} — `;
  const liveSubtitle = live ? "live forecast" : "MSTL daily + weekly + yearly seasonality";
  const subtitle = `${skillSubtitle}${liveSubtitle}`;

  const mapeText = live?.mape != null ? `${(live.mape * 100).toFixed(1)}%` : "8.4%";
  const wapeText = live?.wape != null ? `${(live.wape * 100).toFixed(1)}%` : "6.7%";

  return (
    <>
      <ViewHeader
        title="Forecast"
        subtitle={subtitle}
        right={
          <div className="flex items-center gap-4">
            <GranularityToggle value={granularity} onChange={setGranularity} />
            <div className="text-sm text-text-secondary">
              <span>MAPE </span>
              <span data-mono className="text-text-primary">{mapeText}</span>
              <span className="mx-2 text-text-muted">·</span>
              <span>WAPE </span>
              <span data-mono className="text-text-primary">{wapeText}</span>
            </div>
          </div>
        }
      />
      <ToolResponseRenderer response={response} />
      <div className="mt-4">
        <ToolResponseRenderer response={dowChart} />
      </div>
    </>
  );
}
