"use client";

import { useMemo } from "react";
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

const today = new Date().toISOString().slice(0, 10);
const day = (h: number, m = 0) =>
  `${today}T${h.toString().padStart(2, "0")}:${m.toString().padStart(2, "0")}:00`;

// Synthetic agent roster mirrored from the design's distribution. Primary
// skill is the first entry; secondaries follow.
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

function visibleAgents(filter: SkillKey | typeof ALL_SKILLS) {
  if (filter === ALL_SKILLS) return ROSTER;
  return ROSTER.filter((a) => a.skills[0] === filter);
}

function ganttFor(agents: DemoAgent[]): Extract<ToolResponse, { render: "gantt" }> {
  return {
    render: "gantt",
    date: today,
    agents: agents.map((a) => ({ id: a.id, name: a.name, segments: a.segments })),
  };
}

function requiredBars(filter: SkillKey | typeof ALL_SKILLS):
  Extract<ToolResponse, { render: "chart.bar" }> {
  // 16 half-hour intervals from 08:00–16:00.
  const labels = Array.from({ length: 16 }, (_, i) => {
    return `${(8 + Math.floor(i / 2)).toString().padStart(2, "0")}:${
      i % 2 === 0 ? "00" : "30"
    }`;
  });
  // Per-skill weights match the design's share_baseline; aggregate is the sum.
  const weight: Record<SkillKey, number> = {
    sales: 0.3,
    support: 0.55,
    billing: 0.15,
  };
  const mult = filter === ALL_SKILLS ? 1.0 : weight[filter];
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

export default function SchedulePage() {
  const { skill } = useSkill();
  const agents = useMemo(() => visibleAgents(skill), [skill]);
  const required = useMemo(() => requiredBars(skill), [skill]);
  const gantt = useMemo(() => ganttFor(agents), [agents]);

  // Group by primary skill for the badge legend above the gantt.
  const byPrimary: Record<SkillKey, number> = { sales: 0, support: 0, billing: 0 };
  for (const a of agents) byPrimary[a.skills[0]] += 1;

  const subtitle =
    skill === ALL_SKILLS
      ? "CP-SAT optimized, flexible starts"
      : `Showing agents whose primary skill is ${SKILL_LABEL[skill]}`;

  return (
    <div className="space-y-6">
      <ViewHeader title="Schedule" subtitle={subtitle} />
      <SkillBadgeRow counts={byPrimary} agents={agents} />
      <ToolResponseRenderer response={required} />
      <ToolResponseRenderer response={gantt} />
    </div>
  );
}

function SkillBadgeRow({
  counts,
  agents,
}: {
  counts: Record<SkillKey, number>;
  agents: DemoAgent[];
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
        Total visible: <span data-mono>{agents.length}</span>
      </div>
    </div>
  );
}
