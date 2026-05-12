"use client";

import { useEffect, useMemo, useState } from "react";
import { ToolResponseRenderer } from "@/chat/renderers";
import { useSkill } from "@/context/SkillContext";
import {
  ALL_SKILLS,
  SKILLS,
  SKILL_COLOR,
  SKILL_LABEL,
  type SkillKey,
} from "@/lib/skills";
import { ViewHeader } from "@/components/ViewHeader";
import type { ToolResponse } from "@/chat/types";
import {
  fetchLatestSchedule,
  type ScheduleDashboardData,
} from "@/lib/dashboardData";

const today = new Date().toISOString().slice(0, 10);
const day = (h: number, m = 0) =>
  `${today}T${h.toString().padStart(2, "0")}:${m.toString().padStart(2, "0")}:00`;

// Synthetic fallback roster — mirrors the design's distribution. Used when
// no API base is configured or the backend returns no schedule.
type DemoAgent = {
  id: string;
  name: string;
  skills: SkillKey[];
  segments: Array<{
    start: string;
    end: string;
    activity:
      | "available"
      | "break"
      | "lunch"
      | "training"
      | "meeting"
      | "shrinkage"
      | "off";
  }>;
};

const ROSTER: DemoAgent[] = [
  {
    id: "ag_001",
    name: "Adams, J.",
    skills: ["support"],
    segments: [
      { start: day(8), end: day(12), activity: "available" },
      { start: day(12), end: day(12, 30), activity: "lunch" },
      { start: day(12, 30), end: day(16), activity: "available" },
    ],
  },
  {
    id: "ag_002",
    name: "Becker, M.",
    skills: ["sales", "support"],
    segments: [
      { start: day(9), end: day(11), activity: "available" },
      { start: day(11), end: day(11, 15), activity: "break" },
      { start: day(11, 15), end: day(13), activity: "available" },
      { start: day(13), end: day(14), activity: "training" },
      { start: day(14), end: day(17), activity: "available" },
    ],
  },
  {
    id: "ag_003",
    name: "Chen, R.",
    skills: ["billing"],
    segments: [
      { start: day(10), end: day(13), activity: "available" },
      { start: day(13), end: day(13, 30), activity: "lunch" },
      { start: day(13, 30), end: day(15), activity: "meeting" },
      { start: day(15), end: day(18), activity: "available" },
    ],
  },
  {
    id: "ag_004",
    name: "Diaz, P.",
    skills: ["support", "billing", "sales"],
    segments: [
      { start: day(8), end: day(11, 30), activity: "available" },
      { start: day(11, 30), end: day(12), activity: "break" },
      { start: day(12), end: day(16), activity: "available" },
    ],
  },
];

const SKILL_WEIGHT: Record<SkillKey, number> = {
  sales: 0.3,
  support: 0.55,
  billing: 0.15,
};

function visibleAgents(filter: SkillKey | typeof ALL_SKILLS) {
  if (filter === ALL_SKILLS) return ROSTER;
  return ROSTER.filter((a) => a.skills[0] === filter);
}

function fallbackGantt(agents: DemoAgent[]): Extract<ToolResponse, { render: "gantt" }> {
  return {
    render: "gantt",
    date: today,
    agents: agents.map((a) => ({ id: a.id, name: a.name, segments: a.segments })),
  };
}

function fallbackBars(filter: SkillKey | typeof ALL_SKILLS): Extract<
  ToolResponse,
  { render: "chart.bar" }
> {
  const labels = Array.from({ length: 16 }, (_, i) => {
    return `${(8 + Math.floor(i / 2)).toString().padStart(2, "0")}:${
      i % 2 === 0 ? "00" : "30"
    }`;
  });
  const mult = filter === ALL_SKILLS ? 1.0 : SKILL_WEIGHT[filter];
  const title =
    filter === ALL_SKILLS
      ? "Required vs scheduled (by interval)"
      : `Required vs scheduled — ${SKILL_LABEL[filter]} only`;
  return {
    render: "chart.bar",
    title,
    bars: labels.map((label, i) => ({
      label,
      value: Math.round((8 + Math.sin(i / 2) * 4 + 2) * mult),
    })),
  };
}

// Pick a representative day from the live schedule (the middle day if
// available, else the first day) so the gantt always shows full shifts.
function pickRepresentativeDate(live: ScheduleDashboardData): string {
  const days = new Set<string>();
  for (const s of live.segments) {
    days.add(s.start_time.slice(0, 10));
  }
  const sorted = Array.from(days).sort();
  if (sorted.length === 0) return live.startDate;
  return sorted[Math.floor(sorted.length / 2)];
}

function liveGantt(
  live: ScheduleDashboardData,
  filter: SkillKey | typeof ALL_SKILLS,
  date: string,
): Extract<ToolResponse, { render: "gantt" }> {
  const onDate = live.segments.filter((s) => s.start_time.slice(0, 10) === date);
  // Group by agent.
  const byAgent = new Map<
    number,
    { id: string; name: string; segments: DemoAgent["segments"] }
  >();
  for (const s of onDate) {
    let row = byAgent.get(s.agent_id);
    if (!row) {
      row = {
        id: s.employee_id,
        name: s.full_name,
        segments: [],
      };
      byAgent.set(s.agent_id, row);
    }
    row.segments.push({
      start: s.start_time,
      end: s.end_time,
      activity: s.segment_type,
    });
  }
  // Limit to first 12 agents alphabetically — the gantt becomes unreadable
  // beyond ~15 rows. Filtering by skill would need an agent_skills join
  // we don't have at the dashboard layer yet; show all-skills until that
  // ships.
  void filter;
  const agents = Array.from(byAgent.values())
    .sort((a, b) => a.name.localeCompare(b.name))
    .slice(0, 12);
  return { render: "gantt", date, agents };
}

function liveCoverageBars(
  live: ScheduleDashboardData,
  date: string,
): Extract<ToolResponse, { render: "chart.bar" }> {
  // Bucket coverage rows (typically 30-min) for the picked day into the bar
  // chart. We render REQUIRED bars, matching the original visual where a
  // shortage is implicit (scheduled bars overlay required in the renderer).
  const onDate = live.coverage.filter(
    (c) => c.interval_start.slice(0, 10) === date,
  );
  const bars = onDate.map((c) => {
    const d = new Date(c.interval_start);
    const h = String(d.getUTCHours()).padStart(2, "0");
    const m = String(d.getUTCMinutes()).padStart(2, "0");
    return { label: `${h}:${m}`, value: c.required_agents };
  });
  return {
    render: "chart.bar",
    title: `Required headcount — ${date}`,
    bars,
  };
}

export default function SchedulePage() {
  const { skill } = useSkill();
  const [live, setLive] = useState<ScheduleDashboardData | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchLatestSchedule().then((d) => {
      if (!cancelled) setLive(d);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const date = useMemo(() => (live ? pickRepresentativeDate(live) : today), [live]);

  const fallbackAgents = useMemo(() => visibleAgents(skill), [skill]);
  const fallbackRequired = useMemo(() => fallbackBars(skill), [skill]);
  const fallbackGanttResp = useMemo(
    () => fallbackGantt(fallbackAgents),
    [fallbackAgents],
  );

  const requiredChart = live ? liveCoverageBars(live, date) : fallbackRequired;
  const ganttChart = live ? liveGantt(live, skill, date) : fallbackGanttResp;

  const byPrimary: Record<SkillKey, number> = { sales: 0, support: 0, billing: 0 };
  if (!live) {
    for (const a of fallbackAgents) byPrimary[a.skills[0]] += 1;
  }

  const visibleCount = live ? ganttChart.agents.length : fallbackAgents.length;

  const subtitle = live
    ? `${live.name} · ${live.startDate} → ${live.endDate} · showing ${date}`
    : skill === ALL_SKILLS
      ? "CP-SAT optimized, flexible starts"
      : `Showing agents whose primary skill is ${SKILL_LABEL[skill]}`;

  return (
    <div className="space-y-6">
      <ViewHeader title="Schedule" subtitle={subtitle} />
      {!live && <SkillBadgeRow counts={byPrimary} visibleCount={visibleCount} />}
      <ToolResponseRenderer response={requiredChart} />
      <ToolResponseRenderer response={ganttChart} />
    </div>
  );
}

function SkillBadgeRow({
  counts,
  visibleCount,
}: {
  counts: Record<SkillKey, number>;
  visibleCount: number;
}) {
  return (
    <div className="border border-border-default rounded-md p-3 flex items-center gap-6 text-sm">
      {SKILLS.map((s) => (
        <div key={s} className="flex items-center gap-2">
          <span
            className="inline-block w-2.5 h-2.5 rounded-sm"
            style={{ background: SKILL_COLOR[s] }}
            aria-hidden
          />
          <span className="text-text-primary">{SKILL_LABEL[s]}</span>
          <span data-mono className="text-text-muted">
            {counts[s]} primaries
          </span>
        </div>
      ))}
      <div className="ml-auto text-xs text-text-muted">
        Total visible: <span data-mono>{visibleCount}</span>
      </div>
    </div>
  );
}
