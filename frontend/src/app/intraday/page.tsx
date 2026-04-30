"use client";

import { ToolResponseRenderer } from "@/chat/renderers";
import { ViewHeader } from "@/components/ViewHeader";
import type { ToolResponse } from "@/chat/types";

const xs = Array.from({ length: 48 }, (_, i) => {
  const h = Math.floor(i / 2)
    .toString()
    .padStart(2, "0");
  const m = i % 2 === 0 ? "00" : "30";
  return `${h}:${m}`;
});

const intraday: Extract<ToolResponse, { render: "chart.line" }> = {
  render: "chart.line",
  title: "Today's volume — actual vs forecast",
  yLabel: "calls",
  series: [
    {
      name: "Forecast",
      points: xs.map((x, i) => ({
        x,
        y: 80 + Math.round(Math.sin((i / xs.length) * Math.PI * 2 - 1) * 60 + 40),
      })),
    },
    {
      name: "Actual (so far)",
      points: xs.slice(0, 24).map((x, i) => ({
        x,
        y:
          80 +
          Math.round(Math.sin((i / xs.length) * Math.PI * 2 - 1) * 60 + 40) +
          Math.round(Math.cos(i) * 15),
      })),
    },
  ],
};

export default function IntradayPage() {
  return (
    <>
      <ViewHeader
        title="Intraday"
        subtitle="Today's volume vs forecast, refreshed every 5 minutes"
        right={
          <div className="text-sm text-text-secondary">
            <span>Adherence </span>
            <span data-mono className="text-text-primary">94.2%</span>
          </div>
        }
      />
      <ToolResponseRenderer response={intraday} />
      <p data-mono className="mt-3 text-xs text-text-muted">
        Last refreshed {new Date().toISOString().slice(11, 16)}
      </p>
    </>
  );
}
