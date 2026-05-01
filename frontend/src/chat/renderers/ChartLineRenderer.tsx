"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Legend,
} from "recharts";
import type { ToolResponse } from "../types";

const SERIES_COLORS = ["#0F766E", "#525252", "#A3A3A3"];

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_DATETIME_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;
const YEARMONTH_RE = /^\d{4}-\d{2}$/;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

type XKind = "month" | "date" | "datetime" | "other";

function detectXKind(values: string[]): XKind {
  if (values.length === 0) return "other";
  if (values.every((v) => YEARMONTH_RE.test(v))) return "month";
  if (values.every((v) => ISO_DATE_RE.test(v))) return "date";
  if (values.every((v) => ISO_DATETIME_RE.test(v))) return "datetime";
  return "other";
}

function formatXTick(value: string, kind: XKind, span: number): string {
  if (kind === "month") {
    const [y, m] = value.split("-").map(Number);
    return `${MONTHS[m - 1]} ${String(y).slice(2)}`;
  }
  if (kind === "date") {
    const [y, m, d] = value.split("-").map(Number);
    if (span > 90) return `${MONTHS[m - 1]} ${String(y).slice(2)}`;
    return `${MONTHS[m - 1]} ${d}`;
  }
  if (kind === "datetime") {
    const datePart = value.slice(0, 10);
    const timePart = value.slice(11);
    // Multi-day hourly: midnight ticks show the day; non-midnight show HH:MM.
    if (timePart === "00:00") {
      const [, m, d] = datePart.split("-").map(Number);
      return `${MONTHS[m - 1]} ${d}`;
    }
    return timePart;
  }
  return value;
}

function formatTooltipLabel(value: string, kind: XKind): string {
  if (kind === "month") {
    const [y, m] = value.split("-").map(Number);
    return `${MONTHS[m - 1]} ${y}`;
  }
  if (kind === "date") {
    const [y, m, d] = value.split("-").map(Number);
    return `${MONTHS[m - 1]} ${d}, ${y}`;
  }
  if (kind === "datetime") {
    const [y, m, d] = value.slice(0, 10).split("-").map(Number);
    return `${MONTHS[m - 1]} ${d}, ${y} ${value.slice(11)}`;
  }
  return value;
}

export function ChartLineRenderer({
  response,
}: {
  response: Extract<ToolResponse, { render: "chart.line" }>;
}) {
  const xs = new Set<string>();
  for (const s of response.series) {
    for (const p of s.points) xs.add(p.x);
  }
  const xList = Array.from(xs).sort();
  const data = xList.map((x) => {
    const row: Record<string, string | number> = { x };
    for (const s of response.series) {
      const pt = s.points.find((p) => p.x === x);
      if (pt) row[s.name] = pt.y;
    }
    return row;
  });

  const kind = detectXKind(xList);

  // Pin ticks to natural boundaries for dense series so labels don't get skipped.
  let explicitTicks: string[] | undefined;
  if (kind === "date" && xList.length > 60) {
    explicitTicks = xList.filter((x) => x.endsWith("-01")); // first of each month
  } else if (kind === "datetime" && xList.length > 24) {
    explicitTicks = xList.filter((x) => x.endsWith("T00:00")); // midnight of each day
  }

  const tickInterval =
    explicitTicks !== undefined
      ? 0
      : xList.length > 12
        ? Math.max(0, Math.floor(xList.length / 10) - 1)
        : 0;

  return (
    <figure
      className="border border-border-default rounded-md p-4"
      aria-label={response.title}
    >
      <figcaption className="text-sm text-text-primary mb-3">
        {response.title}
      </figcaption>
      <div>
        <ResponsiveContainer width="100%" height={256} minWidth={0}>
          <LineChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="#E5E5E5" strokeDasharray="2 2" />
            <XAxis
              dataKey="x"
              stroke="#737373"
              fontSize={12}
              tickLine={false}
              interval={tickInterval}
              ticks={explicitTicks}
              tickFormatter={(v: string) => formatXTick(v, kind, xList.length)}
              minTickGap={8}
            />
            <YAxis
              stroke="#737373"
              fontSize={12}
              tickLine={false}
              label={
                response.yLabel
                  ? {
                      value: response.yLabel,
                      angle: -90,
                      position: "insideLeft",
                      style: { fill: "#737373", fontSize: 12 },
                    }
                  : undefined
              }
            />
            <Tooltip
              contentStyle={{
                background: "#FFFFFF",
                border: "1px solid #E5E5E5",
                borderRadius: 6,
                fontSize: 12,
              }}
              labelFormatter={(v) => formatTooltipLabel(String(v), kind)}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            {response.series.map((s, i) => (
              <Line
                key={s.name}
                type="monotone"
                dataKey={s.name}
                stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}
