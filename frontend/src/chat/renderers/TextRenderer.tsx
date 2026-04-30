import type { ToolResponse } from "../types";

export function TextRenderer({
  response,
}: {
  response: Extract<ToolResponse, { render: "text" }>;
}) {
  return (
    <p className="text-sm text-text-primary whitespace-pre-wrap">
      {response.content}
    </p>
  );
}
