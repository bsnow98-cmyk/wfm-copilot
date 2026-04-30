"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ToolResponse } from "../types";

export function ChartBarRenderer({
  response,
}: {
  response: Extract<ToolResponse, { render: "chart.bar" }>;
}) {
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
          <BarChart data={response.bars} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="#E5E5E5" strokeDasharray="2 2" />
            <XAxis
              dataKey="label"
              stroke="#737373"
              fontSize={12}
              tickLine={false}
            />
            <YAxis stroke="#737373" fontSize={12} tickLine={false} />
            <Tooltip
              contentStyle={{
                background: "#FFFFFF",
                border: "1px solid #E5E5E5",
                borderRadius: 6,
                fontSize: 12,
              }}
            />
            <Bar dataKey="value" fill="#0F766E" isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}
