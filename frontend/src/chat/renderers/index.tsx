import type { ToolResponse } from "../types";
import { ChartBarRenderer } from "./ChartBarRenderer";
import { ChartLineRenderer } from "./ChartLineRenderer";
import { ErrorRenderer } from "./ErrorRenderer";
import { GanttRenderer } from "./GanttRenderer";
import { JsonPretty } from "./JsonPretty";
import { ScenariosRenderer } from "./ScenariosRenderer";
import { TableRenderer } from "./TableRenderer";
import { TextRenderer } from "./TextRenderer";

export function ToolResponseRenderer({ response }: { response: ToolResponse }) {
  switch (response.render) {
    case "text":
      return <TextRenderer response={response} />;
    case "chart.line":
      return <ChartLineRenderer response={response} />;
    case "chart.bar":
      return <ChartBarRenderer response={response} />;
    case "table":
      return <TableRenderer response={response} />;
    case "gantt":
      return <GanttRenderer response={response} />;
    case "scenarios":
      return <ScenariosRenderer response={response} />;
    case "error":
      return <ErrorRenderer response={response} />;
    default:
      return <JsonPretty value={response} />;
  }
}

export { JsonPretty };
