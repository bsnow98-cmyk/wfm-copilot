import type { ToolResponse } from "../types";

export function TableRenderer({
  response,
}: {
  response: Extract<ToolResponse, { render: "table" }>;
}) {
  return (
    <figure className="border border-border-default rounded-md overflow-hidden">
      {response.title ? (
        <figcaption className="text-sm text-text-primary px-4 py-3 border-b border-border-default">
          {response.title}
        </figcaption>
      ) : null}
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-label={response.title ?? "Table"}>
          <thead>
            <tr className="bg-surface-subtle text-left text-text-secondary">
              {response.columns.map((c) => (
                <th
                  key={c}
                  className="font-medium px-4 py-2 border-b border-border-default"
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {response.rows.map((row, ri) => (
              <tr
                key={ri}
                className={ri % 2 === 1 ? "bg-surface-subtle" : "bg-surface"}
              >
                {row.map((cell, ci) => (
                  <td
                    key={ci}
                    className="px-4 py-2 align-top"
                    data-mono={typeof cell === "number" ? "" : undefined}
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}
