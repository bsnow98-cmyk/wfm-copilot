"use client";

import { useState } from "react";
import type { GanttActivity, ToolResponse } from "../types";
import { applySchedule, ApplyError } from "@/lib/scheduleApply";

const ACTIVITY_COLOR: Record<GanttActivity, string> = {
  available: "#0F766E",
  break: "#A3A3A3",
  lunch: "#737373",
  training: "#525252",
  meeting: "#0D5F58",
  shrinkage: "#CA8A04",
  off: "#E5E5E5",
};

const ACTIVITY_LABEL: Record<GanttActivity, string> = {
  available: "Available",
  break: "Break",
  lunch: "Lunch",
  training: "Training",
  meeting: "Meeting",
  shrinkage: "Shrinkage",
  off: "Off",
};

const DAY_START_MIN = 0;
const DAY_END_MIN = 24 * 60;

function toMinutes(iso: string): number {
  const d = new Date(iso);
  return d.getHours() * 60 + d.getMinutes();
}

export function GanttRenderer({
  response,
}: {
  response: Extract<ToolResponse, { render: "gantt" }>;
}) {
  const totalMin = DAY_END_MIN - DAY_START_MIN;
  const hours = Array.from({ length: 25 }, (_, i) => i);

  return (
    <figure
      className="border border-border-default rounded-md overflow-hidden"
      aria-label={`Schedule for ${response.date}`}
    >
      <figcaption className="text-sm text-text-primary px-4 py-3 border-b border-border-default flex items-center justify-between">
        <span>
          Schedule <span data-mono className="text-text-muted ml-2">{response.date}</span>
        </span>
        <Legend />
      </figcaption>
      <div className="overflow-x-auto">
        <div className="min-w-[800px]">
          <div className="grid grid-cols-[160px_1fr] border-b border-border-default">
            <div className="px-4 py-2 text-xs text-text-muted">Agent</div>
            <div className="relative h-6">
              {hours.map((h) => (
                <div
                  key={h}
                  data-mono
                  className="absolute top-0 text-xs text-text-muted"
                  style={{ left: `${(h * 60 * 100) / totalMin}%` }}
                >
                  {h.toString().padStart(2, "0")}
                </div>
              ))}
            </div>
          </div>
          {response.agents.map((agent) => (
            <div
              key={agent.id}
              className="grid grid-cols-[160px_1fr] border-b border-border-default last:border-b-0"
            >
              <div className="px-4 py-3 text-sm">
                <div className="text-text-primary">{agent.name}</div>
                <div data-mono className="text-xs text-text-muted">
                  {agent.id}
                </div>
              </div>
              <div className="relative h-10 bg-surface-subtle">
                {agent.segments.map((seg, i) => {
                  const start = toMinutes(seg.start);
                  const end = toMinutes(seg.end);
                  const left = ((start - DAY_START_MIN) * 100) / totalMin;
                  const width = ((end - start) * 100) / totalMin;
                  return (
                    <div
                      key={i}
                      title={`${ACTIVITY_LABEL[seg.activity]} ${seg.start.slice(11, 16)}–${seg.end.slice(11, 16)}`}
                      className="absolute top-1.5 bottom-1.5 rounded-sm"
                      style={{
                        left: `${left}%`,
                        width: `${width}%`,
                        background: ACTIVITY_COLOR[seg.activity],
                      }}
                    />
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
      {response.apply_token && response.schedule_version != null ? (
        <ApplyAffordance
          applyToken={response.apply_token}
          scheduleVersion={response.schedule_version}
          response={response}
        />
      ) : null}
    </figure>
  );
}

function ApplyAffordance({
  applyToken,
  scheduleVersion,
  response,
}: {
  applyToken: string;
  scheduleVersion: number;
  response: Extract<ToolResponse, { render: "gantt" }>;
}) {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "applying" }
    | { kind: "applied"; appliedAt: string }
    | { kind: "conflict"; current: Extract<ToolResponse, { render: "gantt" }> }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  // Reconstruct change_set from the rendered segments. The preview tool
  // already sent the original change_set to the backend when it minted the
  // token; the backend re-validates against the token's stored change_set,
  // so re-deriving from the gantt is fine for the apply payload.
  const changes = response.agents.flatMap((a) =>
    a.segments.map((s) => ({
      agent_id: a.id,
      start: s.start,
      end: s.end,
      activity: s.activity,
    })),
  );

  async function handleApply() {
    setState({ kind: "applying" });
    try {
      const out = await applySchedule({
        apply_token: applyToken,
        schedule_version: scheduleVersion,
        changes,
      });
      setState({ kind: "applied", appliedAt: out.applied_at });
    } catch (err) {
      if (err instanceof ApplyError && err.kind === "conflict" && err.freshPreview) {
        setState({ kind: "conflict", current: err.freshPreview });
        return;
      }
      const message =
        err instanceof Error ? err.message : "Apply failed for an unknown reason.";
      setState({ kind: "error", message });
    }
  }

  if (state.kind === "applied") {
    return (
      <div className="px-4 py-2 border-t border-border-default text-xs text-text-muted">
        Applied <span data-mono>{state.appliedAt.slice(11, 16)}</span>.
      </div>
    );
  }

  if (state.kind === "conflict") {
    return (
      <div className="border-t border-border-default p-4 space-y-3">
        <div className="text-sm text-severity-medium">
          The schedule changed since this preview. Re-preview against the current
          state before applying.
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <div className="text-xs text-text-muted mb-1">Your preview</div>
            <MiniGantt agents={response.agents} />
          </div>
          <div>
            <div className="text-xs text-text-muted mb-1">Current state</div>
            <MiniGantt agents={state.current.agents} />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="px-4 py-2 border-t border-border-default flex items-center gap-3">
      <button
        type="button"
        onClick={handleApply}
        disabled={state.kind === "applying"}
        className="text-sm px-3 py-1.5 rounded-sm bg-accent text-white disabled:bg-border-strong"
        title="Write this change to the schedule"
      >
        {state.kind === "applying" ? "Applying…" : "Apply this change"}
      </button>
      {state.kind === "error" ? (
        <span className="text-xs text-severity-high">{state.message}</span>
      ) : (
        <span className="text-xs text-text-muted">
          A diff will be saved and undoable for 24 hours.
        </span>
      )}
    </div>
  );
}

function MiniGantt({ agents }: { agents: Extract<ToolResponse, { render: "gantt" }>["agents"] }) {
  return (
    <div className="border border-border-default rounded-sm overflow-hidden text-xs">
      {agents.map((a) => (
        <div
          key={a.id}
          className="grid grid-cols-[80px_1fr] border-b border-border-default last:border-b-0"
        >
          <div className="px-2 py-1 truncate">{a.name}</div>
          <div className="relative h-5 bg-surface-subtle">
            {a.segments.map((s, i) => {
              const start = new Date(s.start);
              const end = new Date(s.end);
              const minutes = (d: Date) => d.getHours() * 60 + d.getMinutes();
              const left = (minutes(start) * 100) / (24 * 60);
              const width = ((minutes(end) - minutes(start)) * 100) / (24 * 60);
              return (
                <div
                  key={i}
                  className="absolute top-0.5 bottom-0.5 rounded-sm"
                  style={{
                    left: `${left}%`,
                    width: `${width}%`,
                    background: ACTIVITY_COLOR[s.activity],
                  }}
                />
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

function Legend() {
  const items: GanttActivity[] = [
    "available",
    "break",
    "lunch",
    "training",
    "meeting",
    "shrinkage",
    "off",
  ];
  return (
    <ul className="flex gap-3 text-xs text-text-muted">
      {items.map((a) => (
        <li key={a} className="flex items-center gap-1.5">
          <span
            className="inline-block w-2.5 h-2.5 rounded-sm"
            style={{ background: ACTIVITY_COLOR[a] }}
            aria-hidden
          />
          {ACTIVITY_LABEL[a]}
        </li>
      ))}
    </ul>
  );
}
