export function JsonPretty({ value }: { value: unknown }) {
  return (
    <pre
      data-mono
      data-testid="wfm-jsonpretty"
      className="text-xs bg-surface-subtle border border-border-default rounded-md p-3 overflow-x-auto"
    >
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}
