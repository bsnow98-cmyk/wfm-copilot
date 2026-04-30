export function ViewHeader({
  title,
  subtitle,
  right,
}: {
  title: string;
  subtitle?: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between mb-6">
      <div>
        <h1 className="text-xl font-medium text-text-primary">{title}</h1>
        {subtitle ? (
          <p className="text-sm text-text-muted mt-1">{subtitle}</p>
        ) : null}
      </div>
      {right ? <div className="ml-4 shrink-0">{right}</div> : null}
    </div>
  );
}
