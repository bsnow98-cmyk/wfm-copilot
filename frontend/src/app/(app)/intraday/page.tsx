"use client";

import { useEffect, useState } from "react";
import { ToolResponseRenderer } from "@/chat/renderers";
import { ViewHeader } from "@/components/ViewHeader";
import type { ToolResponse } from "@/chat/types";
import {
  fetchIntradayToday,
  type IntradayDashboardData,
} from "@/lib/dashboardData";

const FALLBACK_XS = Array.from({ length: 48 }, (_, i) => {
  const h = Math.floor(i / 2)
    .toString()
    .padStart(2, "0");
  const m = i % 2 === 0 ? "00" : "30";
  return `${h}:${m}`;
});

const FALLBACK_RESPONSE: Extract<ToolResponse, { render: "chart.line" }> = {
  render: "chart.line",
  title: "Today's volume — actual vs forecast",
  yLabel: "calls",
  series: [
    {
      name: "Forecast",
      points: FALLBACK_XS.map((x, i) => ({
        x,
        y: 80 + Math.round(Math.sin((i / FALLBACK_XS.length) * Math.PI * 2 - 1) * 60 + 40),
      })),
    },
    {
      name: "Actual (so far)",
      points: FALLBACK_XS.slice(0, 24).map((x, i) => ({
        x,
        y:
          80 +
          Math.round(Math.sin((i / FALLBACK_XS.length) * Math.PI * 2 - 1) * 60 + 40) +
          Math.round(Math.cos(i) * 15),
      })),
    },
  ],
};

function buildLiveResponse(
  live: IntradayDashboardData,
): Extract<ToolResponse, { render: "chart.line" }> {
  const date = live.simNow.slice(0, 10);
  const forecastPts = live.points
    .filter((p) => p.forecast != null)
    .map((p) => ({
      x: p.interval_start.slice(11, 16),
      y: Math.round(p.forecast ?? 0),
    }));
  const actualPts = live.points
    .filter((p) => p.actual != null)
    .map((p) => ({
      x: p.interval_start.slice(11, 16),
      y: Math.round(p.actual ?? 0),
    }));
  return {
    render: "chart.line",
    title: `Today's volume — ${date} · queue ${live.queue}`,
    yLabel: "calls",
    series: [
      { name: "Forecast", points: forecastPts },
      { name: "Actual (so far)", points: actualPts },
    ],
  };
}

export default function IntradayPage() {
  const [live, setLive] = useState<IntradayDashboardData | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchIntradayToday().then((d) => {
      if (!cancelled) setLive(d);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const response = live ? buildLiveResponse(live) : FALLBACK_RESPONSE;
  const refreshedAt = live
    ? live.simNow.slice(11, 16)
    : new Date().toISOString().slice(11, 16);
  const subtitle = live
    ? `Live intraday vs forecast · sim-now ${live.simNow.slice(0, 16).replace("T", " ")}`
    : "Today's volume vs forecast, refreshed every 5 minutes";

  return (
    <>
      <ViewHeader
        title="Intraday"
        subtitle={subtitle}
        right={
          <div className="text-sm text-text-secondary">
            <span>Adherence </span>
            <span data-mono className="text-text-primary">94.2%</span>
          </div>
        }
      />
      <ToolResponseRenderer response={response} />
      <p data-mono className="mt-3 text-xs text-text-muted">
        Last refreshed {refreshedAt}
      </p>
    </>
  );
}
