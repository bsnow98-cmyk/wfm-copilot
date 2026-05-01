import Link from "next/link";

export default function Landing() {
  return (
    <main className="relative min-h-screen w-full overflow-hidden bg-[#0A0A0A] text-white">
      <BackgroundForecast />
      <div className="absolute inset-0 bg-[#0A0A0A]/55" aria-hidden />
      <div className="absolute inset-0 bg-gradient-to-b from-[#0A0A0A]/30 via-transparent to-[#0A0A0A]/95" aria-hidden />

      <section className="relative z-10 flex min-h-screen flex-col items-center justify-center px-6 text-center">
        <h1 className="font-sans text-5xl font-semibold leading-[1.05] tracking-tight sm:text-6xl md:text-7xl">
          WFM Copilot
        </h1>

        <p className="mt-6 max-w-2xl text-base leading-relaxed text-white/80 sm:text-lg">
          The AI shows its math. Forecasts, schedules, and answers — built for
          contact center supervisors who need the work shown, not just the
          answer.
        </p>

        <Link
          href="/forecast"
          className="mt-10 inline-flex items-center gap-2 rounded-full bg-white px-8 py-3.5 text-base font-medium text-[#0A0A0A] transition-colors hover:bg-white/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 focus-visible:ring-offset-[#0A0A0A]"
        >
          Try the demo
          <span aria-hidden>→</span>
        </Link>

        <p className="mt-8 text-xs text-white/50">
          Open source.{" "}
          <a
            href="https://github.com/bsnow98-cmyk/wfm-copilot"
            className="underline-offset-4 hover:underline"
          >
            github.com/bsnow98-cmyk/wfm-copilot
          </a>
        </p>
      </section>
    </main>
  );
}

function BackgroundForecast() {
  return (
    <svg
      aria-hidden
      className="absolute inset-0 h-full w-full opacity-95 [filter:blur(1.5px)]"
      viewBox="0 0 1440 900"
      preserveAspectRatio="xMidYMid slice"
    >
      <defs>
        <linearGradient id="line-fade" x1="0" x2="1" y1="0" y2="0">
          <stop offset="0" stopColor="#0F766E" stopOpacity="0" />
          <stop offset="0.15" stopColor="#0F766E" stopOpacity="0.9" />
          <stop offset="0.85" stopColor="#0F766E" stopOpacity="0.9" />
          <stop offset="1" stopColor="#0F766E" stopOpacity="0" />
        </linearGradient>
        <linearGradient id="line-fade-dim" x1="0" x2="1" y1="0" y2="0">
          <stop offset="0" stopColor="#0F766E" stopOpacity="0" />
          <stop offset="0.15" stopColor="#0F766E" stopOpacity="0.4" />
          <stop offset="0.85" stopColor="#0F766E" stopOpacity="0.4" />
          <stop offset="1" stopColor="#0F766E" stopOpacity="0" />
        </linearGradient>
      </defs>

      {Array.from({ length: 13 }).map((_, i) => {
        const y = 60 + i * 24;
        return (
          <line
            key={`grid-${i}`}
            x1="80"
            x2="1360"
            y1={y * 2.5}
            y2={y * 2.5}
            stroke="#FFFFFF"
            strokeOpacity="0.04"
            strokeWidth="1"
          />
        );
      })}

      <ForecastLine baseY={560} amplitude={150} period={70} phase={0} stroke="url(#line-fade-dim)" strokeWidth={1.75} />
      <ForecastLine baseY={460} amplitude={95} period={60} phase={20} stroke="url(#line-fade)" strokeWidth={2.25} />
      <ForecastLine baseY={360} amplitude={55} period={55} phase={45} stroke="url(#line-fade-dim)" strokeWidth={1.75} />
    </svg>
  );
}

function ForecastLine({
  baseY,
  amplitude,
  period,
  phase,
  stroke,
  strokeWidth = 1.25,
}: {
  baseY: number;
  amplitude: number;
  period: number;
  phase: number;
  stroke: string;
  strokeWidth?: number;
}) {
  const points: string[] = [];
  for (let x = 80; x <= 1360; x += 4) {
    const t = (x - 80) / period;
    const y =
      baseY +
      Math.sin(t + phase * 0.1) * amplitude +
      Math.sin(t * 0.37 + phase * 0.2) * amplitude * 0.35 +
      Math.sin(t * 2.1 + phase * 0.5) * amplitude * 0.12;
    points.push(`${x},${y.toFixed(1)}`);
  }
  return (
    <polyline
      points={points.join(" ")}
      fill="none"
      stroke={stroke}
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  );
}
