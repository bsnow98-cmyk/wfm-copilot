import type { ToolResponse } from "../types";

export function ErrorRenderer({
  response,
}: {
  response: Extract<ToolResponse, { render: "error" }>;
}) {
  return (
    <div
      role="alert"
      className="border border-severity-high/40 bg-severity-high/5 rounded-md p-3 text-sm"
    >
      <div className="text-severity-high font-medium">{response.message}</div>
      {response.code ? (
        <div data-mono className="mt-1 text-xs text-text-muted">
          {response.code}
        </div>
      ) : null}
    </div>
  );
}
