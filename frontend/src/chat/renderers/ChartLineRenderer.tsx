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
