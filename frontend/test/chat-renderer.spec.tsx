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

  it("renders a plain table without a decision affordance", () => {
    render(<ToolResponseRenderer response={SAMPLES.table} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders an Approve affordance when a table carries a leave_decision", () => {
    const sample: ToolResponse = {
      render: "table",
      title: "Approve PTO — Adams (EMP001)",
      columns: ["Day", "Verdict"],
      rows: [["2026-06-14", "OK"]],
      apply_token: "tok-abc",
      leave_decision: {
        request_id: 3,
        decision: "approve",
        request_version: 123,
        label: "Adams (EMP001)",
        pto_hours: 40,
      },
    };
    render(<ToolResponseRenderer response={sample} />);
    expect(
      screen.getByRole("button", { name: /Approve Adams \(EMP001\)/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Charges 40h PTO/)).toBeInTheDocument();
  });

  it("renders a Publish affordance when a table carries an offer", () => {
    const sample: ToolResponse = {
      render: "table",
      title: "Publish OT offer — 2026-06-09",
      columns: ["rank", "agent"],
      rows: [[1, "Agent 001"]],
      apply_token: "tok-offer",
      offer: {
        kind: "ot",
        slots: 3,
        n_targets: 9,
        window_label: "09:00–12:00",
        target_date: "2026-06-09",
      },
    };
    render(<ToolResponseRenderer response={sample} />);
    expect(
      screen.getByRole("button", { name: /Publish OT offer to 9 agents/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/09:00–12:00 · 3 slots/)).toBeInTheDocument();
  });

  it("renders an Override affordance when a table carries a forecast_override", () => {
    const sample: ToolResponse = {
      render: "table",
      title: "Override forecast — all, 2026-06-14 14:00",
      columns: ["Interval", "Current offered", "Proposed", "Δ"],
      rows: [["2026-06-14 14:00", "270", "320", "+50"]],
      apply_token: "tok-fc",
      forecast_override: {
        queue: "all",
        interval_label: "2026-06-14 14:00",
        current: 270,
        proposed: 320,
      },
    };
    render(<ToolResponseRenderer response={sample} />);
    expect(
      screen.getByRole("button", { name: /Override to 320/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Pins all 2026-06-14 14:00/)).toBeInTheDocument();
  });

  it("renders a staffing-target affordance when a table carries staffing_target", () => {
    const sample: ToolResponse = {
      render: "table",
      title: "Change staffing target — all",
      columns: ["Metric", "Current", "Proposed"],
      rows: [["Service level", "80%", "85%"]],
      apply_token: "tok-st",
      staffing_target: { queue: "all", peak_before: 38, peak_after_est: 39 },
    };
    render(<ToolResponseRenderer response={sample} />);
    expect(
      screen.getByRole("button", { name: /Apply targets & recompute/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/background job/)).toBeInTheDocument();
  });

  it("renders an Award affordance when a table carries a vacation_award", () => {
    const sample: ToolResponse = {
      render: "table",
      title: "Award preview — round 1",
      columns: ["Seniority", "Agent", "Week"],
      rows: [[1, "Adams", "2027-01-04"]],
      apply_token: "tok-vac",
      vacation_award: {
        round_id: 1,
        n_awarded: 42,
        n_agents: 30,
        n_zero_win: 5,
        n_denied: 18,
        weeks_at_capacity: 6,
      },
    };
    render(<ToolResponseRenderer response={sample} />);
    expect(
      screen.getByRole("button", { name: /Award 42 weeks to 30 agents/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/5 get nothing · 6 weeks maxed/)).toBeInTheDocument();
  });
});
