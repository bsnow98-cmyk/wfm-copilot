import type { ToolResponse } from "../types";

export function ScenariosRenderer({
  response,
}: {
  response: Extract<ToolResponse, { render: "scenarios" }>;
}) {
  const intervalCount = Math.max(
    0,
    ...response.scenarios.map((s) => s.required_by_interval.length),
  );

  return (
    <figure
      className="border border-border-default rounded-md overflow-hidden"
      aria-label="Scenario comparison"
    >
      <div className="grid" style={{ gridTemplateColumns: `repeat(${response.scenarios.length}, minmax(0, 1fr))` }}>
        {response.scenarios.map((s) => {
          const total = s.required_by_interval.reduce((a, b) => a + b, 0);
          return (
            <div
              key={s.name}
              className="border-r border-border-default last:border-r-0 p-4"
            >
              <h4 className="text-sm font-medium text-text-primary mb-2">
                {s.name}
              </h4>
              <dl className="text-xs text-text-secondary space-y-1">
                <div className="flex justify-between">
                  <dt>SL target</dt>
                  <dd data-mono>{(s.sla * 100).toFixed(0)}%</dd>
                </div>
                <div className="flex justify-between">
                  <dt>ASA</dt>
                  <dd data-mono>{s.asa_seconds}s</dd>
                </div>
                <div className="flex justify-between">
                  <dt>Total required</dt>
                  <dd data-mono>{total}</dd>
                </div>
                <div className="flex justify-between">
                  <dt>Intervals</dt>
                  <dd data-mono>{s.required_by_interval.length}</dd>
                </div>
              </dl>
              <Sparkbar values={s.required_by_interval} maxLength={intervalCount} />
            </div>
          );
        })}
      </div>
    </figure>
  );
}

function Sparkbar({
  values,
  maxLength,
}: {
  values: number[];
  maxLength: number;
}) {
  const max = Math.max(1, ...values);
  return (
    <div className="mt-3 flex items-end gap-px h-12">
      {Array.from({ length: maxLength }).map((_, i) => {
        const v = values[i] ?? 0;
        return (
          <span
            key={i}
            className="flex-1 bg-accent"
            style={{ height: `${(v / max) * 100}%`, minHeight: 1 }}
          />
        );
      })}
    </div>
  );
}
