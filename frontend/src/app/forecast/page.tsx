"use client";

import { useMemo } from "react";
import { ToolResponseRenderer } from "@/chat/renderers";
import { useSkill } from "@/context/SkillContext";
import { ALL_SKILLS, SKILLS, SKILL_LABEL, type SkillKey } from "@/lib/skills";
import { ViewHeader } from "@/components/ViewHeader";
import type { ToolResponse } from "@/chat/types";

// Per-skill multipliers approximating the synthetic data generator's
// SKILL_PROFILES.share_baseline. Sums to ~1.0 — multiplying the queue
// curve by these reproduces the per-skill curves that backend
// generate_per_skill emits.
const SKILL_WEIGHT: Record<SkillKey, number> = {
  sales: 0.30,
  support: 0.55,
  billing: 0.15,
};

function queueCurve(): { x: string; y: number }[] {
  return Array.from({ length: 30 }, (_, i) => ({
    x: `D-${30 - i}`,
    y: 1200 + Math.round(Math.sin(i / 3) * 300 + i * 8),
  }));
}

function actualCurve(): { x: string; y: number }[] {
  return Array.from({ length: 30 }, (_, i) => ({
    x: `D-${30 - i}`,
    y: 1200 + Math.round(Math.sin(i / 3) * 280 + i * 8 + Math.cos(i) * 60),
  }));
}

function multiCurveResponse(): Extract<ToolResponse, { render: "chart.line" }> {
  const queue = queueCurve();
  const series = SKILLS.map((s) => ({
    name: SKILL_LABEL[s],
    points: queue.map((p) => ({ x: p.x, y: Math.round(p.y * SKILL_WEIGHT[s]) })),
  }));
  return {
    render: "chart.line",
    title: "Forecast by skill — sales_inbound, last 30 days",
    yLabel: "calls",
    series,
  };
}

function singleSkillResponse(skill: SkillKey): Extract<ToolResponse, { render: "chart.line" }> {
  const queue = queueCurve();
  const actual = actualCurve();
  const w = SKILL_WEIGHT[skill];
  return {
    render: "chart.line",
    title: `Forecast vs Actual — ${SKILL_LABEL[skill]}, last 30 days`,
    yLabel: "calls",
    series: [
      {
        name: "Forecast",
        points: queue.map((p) => ({ x: p.x, y: Math.round(p.y * w) })),
      },
      {
        name: "Actual",
        points: actual.map((p) => ({ x: p.x, y: Math.round(p.y * w) })),
      },
    ],
  };
}

export default function ForecastPage() {
  const { skill } = useSkill();
  const response = useMemo(
    () => (skill === ALL_SKILLS ? multiCurveResponse() : singleSkillResponse(skill)),
    [skill],
  );
  const subtitle =
    skill === ALL_SKILLS
      ? "MSTL daily + weekly seasonality, split by historical skill mix"
      : `Filtered to ${SKILL_LABEL[skill]} — MSTL daily + weekly seasonality`;

  return (
    <>
      <ViewHeader
        title="Forecast"
        subtitle={subtitle}
        right={
          <div className="text-sm text-text-secondary">
            <span>MAPE </span>
            <span data-mono className="text-text-primary">8.4%</span>
            <span className="mx-2 text-text-muted">·</span>
            <span>WAPE </span>
            <span data-mono className="text-text-primary">6.7%</span>
          </div>
        }
      />
      <ToolResponseRenderer response={response} />
    </>
  );
}
