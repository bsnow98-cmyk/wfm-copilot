"use client";

import { useMemo } from "react";
import { ToolResponseRenderer } from "@/chat/renderers";
import { useSkill } from "@/context/SkillContext";
import { ALL_SKILLS, SKILL_LABEL, type SkillKey } from "@/lib/skills";
import { ViewHeader } from "@/components/ViewHeader";
import type { ToolResponse } from "@/chat/types";

const required = (mult: number) =>
  Array.from({ length: 24 }, (_, i) =>
    Math.round((6 + Math.sin(i / 3) * 3 + 3) * mult),
  );

// Same per-skill share weights as forecast/schedule pages.
const SKILL_WEIGHT: Record<SkillKey, number> = {
  sales: 0.30,
  support: 0.55,
  billing: 0.15,
};

function buildScenarios(filter: SkillKey | typeof ALL_SKILLS):
  Extract<ToolResponse, { render: "scenarios" }> {
  const skillMult = filter === ALL_SKILLS ? 1.0 : SKILL_WEIGHT[filter];
  return {
    render: "scenarios",
    scenarios: [
      {
        name: "Baseline (80/20)",
        required_by_interval: required(1.0 * skillMult),
        sla: 0.8,
        asa_seconds: 20,
      },
      {
        name: "Tight (90/15)",
        required_by_interval: required(1.18 * skillMult),
        sla: 0.9,
        asa_seconds: 15,
      },
      {
        name: "Loose (70/30)",
        required_by_interval: required(0.86 * skillMult),
        sla: 0.7,
        asa_seconds: 30,
      },
    ],
  };
}

export default function ScenariosPage() {
  const { skill } = useSkill();
  const response = useMemo(() => buildScenarios(skill), [skill]);
  const subtitle =
    skill === ALL_SKILLS
      ? "Side-by-side staffing under different SL / ASA / shrinkage."
      : `Scenarios scoped to ${SKILL_LABEL[skill]} demand only.`;

  return (
    <>
      <ViewHeader title="Scenarios" subtitle={subtitle} />
      <ToolResponseRenderer response={response} />
      <button
        type="button"
        className="mt-4 text-sm text-text-secondary border border-dashed border-border-default rounded-sm px-3 py-2 hover:text-text-primary"
      >
        + Add scenario
      </button>
    </>
  );
}
