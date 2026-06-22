export type Severity = "low" | "medium" | "high";

export type GanttActivity =
  | "available"
  | "break"
  | "lunch"
  | "training"
  | "meeting"
  | "shrinkage"
  | "off";

export type LinePoint = { x: string; y: number };
export type LineSeries = { name: string; points: LinePoint[] };

export type Bar = { label: string; value: number };

export type GanttSegment = {
  start: string;
  end: string;
  activity: GanttActivity;
};

export type GanttAgent = {
  id: string;
  name: string;
  segments: GanttSegment[];
};

export type LeaveDecisionPreview = {
  request_id: number;
  decision: "approve" | "deny";
  request_version: number;
  label: string;
  note?: string | null;
  pto_hours?: number | null;
};

export type OfferPreview = {
  kind: "ot" | "vto";
  slots: number;
  n_targets: number;
  window_label: string;
  target_date: string;
  message?: string | null;
};

export type StaffingTargetPreview = {
  queue: string;
  peak_before: number;
  peak_after_est: number;
};

export type Scenario = {
  name: string;
  required_by_interval: number[];
  sla: number;
  asa_seconds: number;
};

export type ToolResponse =
  | { render: "text"; content: string }
  | {
      render: "chart.line";
      title: string;
      yLabel?: string;
      series: LineSeries[];
    }
  | { render: "chart.bar"; title: string; bars: Bar[] }
  | {
      render: "table";
      title?: string;
      columns: string[];
      rows: (string | number)[][];
      // Surfaces #1/#2 — present only when this table comes from a preview
      // tool. The renderer shows the matching write affordance when apply_token
      // plus the relevant context block are set.
      apply_token?: string;
      leave_decision?: LeaveDecisionPreview; // preview_leave_decision (#1)
      offer?: OfferPreview; // preview_offer (#2)
      staffing_target?: StaffingTargetPreview; // preview_staffing_target (#5)
    }
  | {
      render: "gantt";
      date: string;
      agents: GanttAgent[];
      // Cherry-pick D — present only when this gantt comes from
      // preview_schedule_change AND the date has a schedule to write into.
      // The renderer shows an Apply button when both fields are set.
      apply_token?: string;
      schedule_version?: number;
    }
  | { render: "scenarios"; scenarios: Scenario[] }
  | { render: "error"; message: string; code?: string };

export type RenderType = ToolResponse["render"];

export type ToolCall = {
  tool_name:
    | "get_forecast"
    | "get_staffing"
    | "get_schedule"
    | "get_anomalies"
    | "compare_scenarios"
    | "preview_schedule_change";
  arguments: Record<string, unknown>;
};

export type ChatTurn =
  | { role: "user"; id: string; content: string; created_at: string }
  | {
      role: "assistant";
      id: string;
      content: string;
      created_at: string;
      tool_calls?: ToolCall[];
      tool_results?: ToolResponse[];
    };
