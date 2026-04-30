import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ToolResponseRenderer } from "@/chat/renderers";
import type { ToolResponse } from "@/chat/types";

const MARKER = "wfm-jsonpretty";

const SAMPLES: Record<ToolResponse["render"], ToolResponse> = {
  text: { render: "text", content: "hello world" },
  "chart.line": {
    render: "chart.line",
    title: "Forecast vs Actual",
    series: [
      { name: "Forecast", points: [{ x: "00:00", y: 1 }] },
      { name: "Actual", points: [{ x: "00:00", y: 2 }] },
    ],
  },
  "chart.bar": {
    render: "chart.bar",
    title: "Required by interval",
    bars: [{ label: "08:00", value: 5 }],
  },
  table: {
    render: "table",
    title: "Anomalies",
    columns: ["id", "score"],
    rows: [["a3f291d4", 4.2]],
  },
  gantt: {
    render: "gantt",
    date: "2026-04-29",
    agents: [
      {
        id: "ag_001",
        name: "Adams, J.",
        segments: [
          {
            start: "2026-04-29T09:00:00",
            end: "2026-04-29T12:00:00",
            activity: "available",
          },
        ],
      },
    ],
  },
  scenarios: {
    render: "scenarios",
    scenarios: [
      {
        name: "Baseline",
        required_by_interval: [1, 2, 3],
        sla: 0.8,
        asa_seconds: 20,
      },
    ],
  },
  error: { render: "error", message: "Solver timed out", code: "SOLVER_TIMEOUT" },
};

describe("ToolResponseRenderer", () => {
  it("has a sample for every ToolResponse render type", () => {
    expect(Object.keys(SAMPLES)).toHaveLength(7);
  });

  it.each(Object.entries(SAMPLES))(
    "renders %s via a typed renderer (not JsonPretty)",
    (_label, sample) => {
      const { container } = render(<ToolResponseRenderer response={sample} />);
      const json = container.querySelector(`[data-testid="${MARKER}"]`);
      expect(json).toBeNull();
      expect(container.firstChild).not.toBeNull();
    },
  );

  it("falls back to JsonPretty for an unknown render value", () => {
    const unknown = { render: "future.shape", payload: 42 } as unknown as ToolResponse;
    const { container } = render(<ToolResponseRenderer response={unknown} />);
    expect(container.querySelector("pre")).not.toBeNull();
  });

  it("renders text content", () => {
    render(<ToolResponseRenderer response={SAMPLES.text} />);
    expect(screen.getByText("hello world")).toBeInTheDocument();
  });

  it("renders error message and code", () => {
    render(<ToolResponseRenderer response={SAMPLES.error} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Solver timed out")).toBeInTheDocument();
    expect(screen.getByText("SOLVER_TIMEOUT")).toBeInTheDocument();
  });
});
