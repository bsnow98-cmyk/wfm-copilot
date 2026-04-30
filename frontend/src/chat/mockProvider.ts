import type { ChatHandlers, ChatProvider, ChatProviderRequest, ChatProviderResult } from "./provider";
import type { ToolCall, ToolResponse } from "./types";

const ISO_TODAY = new Date().toISOString().slice(0, 10);

function intervalsForToday(count = 48): string[] {
  const out: string[] = [];
  for (let i = 0; i < count; i++) {
    const h = Math.floor(i / 2)
      .toString()
      .padStart(2, "0");
    const m = i % 2 === 0 ? "00" : "30";
    out.push(`${h}:${m}`);
  }
  return out;
}

function fakeForecast(): Extract<ToolResponse, { render: "chart.line" }> {
  const xs = intervalsForToday();
  const baseline = (i: number) =>
    Math.round(80 + 60 * Math.sin((i / xs.length) * Math.PI * 2 - 1) + 40);
  return {
    render: "chart.line",
    title: "Forecast vs Actual — sales_inbound",
    yLabel: "calls",
    series: [
      { name: "Forecast", points: xs.map((x, i) => ({ x, y: baseline(i) })) },
      {
        name: "Actual",
        points: xs.map((x, i) => ({
          x,
          y: Math.max(0, baseline(i) + Math.round(Math.sin(i) * 12)),
        })),
      },
    ],
  };
}

function fakeStaffing(): Extract<ToolResponse, { render: "chart.bar" }> {
  const xs = intervalsForToday(12);
  return {
    render: "chart.bar",
    title: "Required agents by interval",
    bars: xs.map((label, i) => ({
      label,
      value: 6 + Math.round(Math.sin(i / 2) * 3 + 3),
    })),
  };
}

function fakeAnomalies(): Extract<ToolResponse, { render: "table" }> {
  return {
    render: "table",
    title: "Anomalies — last 7 days",
    columns: ["id", "date", "queue", "category", "severity", "score"],
    rows: [
      ["a3f291d4ce2811af", "2026-04-26", "sales_inbound", "volume_spike", "high", 4.21],
      ["b7c182edab904432", "2026-04-27", "billing", "low_volume", "medium", 2.87],
      ["c91a7702ff63de10", "2026-04-28", "support", "aht_drift", "low", 1.12],
    ],
  };
}

function fakeSchedule(): Extract<ToolResponse, { render: "gantt" }> {
  const date = ISO_TODAY;
  const day = (h: number, m = 0) =>
    `${date}T${h.toString().padStart(2, "0")}:${m.toString().padStart(2, "0")}:00`;
  return {
    render: "gantt",
    date,
    agents: [
      {
        id: "ag_001",
        name: "Adams, J.",
        segments: [
          { start: day(8), end: day(12), activity: "available" },
          { start: day(12), end: day(12, 30), activity: "lunch" },
          { start: day(12, 30), end: day(16), activity: "available" },
        ],
      },
      {
        id: "ag_002",
        name: "Becker, M.",
        segments: [
          { start: day(9), end: day(11), activity: "available" },
          { start: day(11), end: day(11, 15), activity: "break" },
          { start: day(11, 15), end: day(13), activity: "available" },
          { start: day(13), end: day(14), activity: "training" },
          { start: day(14), end: day(17), activity: "available" },
        ],
      },
    ],
  };
}

function fakeScenarios(): Extract<ToolResponse, { render: "scenarios" }> {
  const required = (mult: number) =>
    intervalsForToday(24).map((_, i) => Math.round((6 + Math.sin(i / 3) * 3 + 3) * mult));
  return {
    render: "scenarios",
    scenarios: [
      { name: "Baseline (80/20)", required_by_interval: required(1.0), sla: 0.8, asa_seconds: 20 },
      { name: "Tight (90/15)", required_by_interval: required(1.18), sla: 0.9, asa_seconds: 15 },
      { name: "Loose (70/30)", required_by_interval: required(0.86), sla: 0.7, asa_seconds: 30 },
    ],
  };
}

type Picked = { call: ToolCall; result: ToolResponse; summary: string };

function pickTool(message: string): Picked {
  const m = message.toLowerCase();
  if (m.includes("forecast"))
    return {
      call: { tool_name: "get_forecast", arguments: { date: ISO_TODAY, queue: "sales_inbound" } },
      result: fakeForecast(),
      summary: "Pulling today's forecast for sales_inbound.",
    };
  if (m.includes("staffing") || m.includes("required"))
    return {
      call: { tool_name: "get_staffing", arguments: { sl: 0.8, asa: 20 } },
      result: fakeStaffing(),
      summary: "Required agents by interval at 80/20.",
    };
  if (m.includes("anomal"))
    return {
      call: { tool_name: "get_anomalies", arguments: { date: ISO_TODAY } },
      result: fakeAnomalies(),
      summary: "Anomalies from the last 7 days.",
    };
  if (m.includes("schedule") || m.includes("gantt"))
    return {
      call: { tool_name: "get_schedule", arguments: { date: ISO_TODAY } },
      result: fakeSchedule(),
      summary: "Today's schedule.",
    };
  if (m.includes("scenario") || m.includes("compare"))
    return {
      call: { tool_name: "compare_scenarios", arguments: {} },
      result: fakeScenarios(),
      summary: "Three scenarios side by side.",
    };
  return {
    call: { tool_name: "get_forecast", arguments: { hint: "default" } },
    result: {
      render: "text",
      content:
        "Mock provider — try asking about forecast, staffing, schedule, anomalies, or scenarios.",
    },
    summary: "",
  };
}

async function delay(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

export const mockProvider: ChatProvider = {
  async send(req: ChatProviderRequest, handlers: ChatHandlers): Promise<ChatProviderResult> {
    const conversation_id =
      req.conversation_id ?? `conv_${Math.random().toString(36).slice(2, 10)}`;
    const picked = pickTool(req.message);

    // Stream a few tokens so the UI exercises the streaming path.
    for (const chunk of picked.summary.split(" ")) {
      await delay(40);
      handlers.onToken?.(chunk + " ");
    }

    if (picked.call) {
      await delay(80);
      handlers.onToolCall?.(picked.call);
      await delay(160);
      handlers.onToolResult?.(picked.call.tool_name, picked.result);
    }

    return { conversation_id };
  },
};
