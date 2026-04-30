"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/cn";
import { useSkill } from "@/context/SkillContext";
import { ALL_SKILLS, SKILL_LABEL, SKILLS, type SkillFilter } from "@/lib/skills";
import { NotificationBell } from "./NotificationBell";

const NAV = [
  { href: "/forecast", label: "Forecast" },
  { href: "/schedule", label: "Schedule" },
  { href: "/intraday", label: "Intraday" },
  { href: "/scenarios", label: "Scenarios" },
];

function SkillPicker() {
  const { skill, setSkill } = useSkill();
  return (
    <label className="flex items-center gap-2 text-sm text-text-secondary">
      <span className="text-xs text-text-muted">Skill</span>
      <select
        value={skill}
        onChange={(e) => setSkill(e.target.value as SkillFilter)}
        aria-label="Filter views by skill"
        className="text-sm border border-border-default rounded-sm px-2 py-1.5 bg-surface focus:outline-none focus:border-accent"
      >
        <option value={ALL_SKILLS}>All skills</option>
        {SKILLS.map((s) => (
          <option key={s} value={s}>
            {SKILL_LABEL[s]}
          </option>
        ))}
      </select>
    </label>
  );
}

export function TopNav({
  onToggleChat,
  chatOpen,
}: {
  onToggleChat: () => void;
  chatOpen: boolean;
}) {
  const pathname = usePathname();
  return (
    <header className="h-14 border-b border-border-default bg-surface flex items-center px-6 shrink-0">
      <Link
        href="/"
        className="text-base font-medium tracking-tight text-text-primary mr-8"
      >
        WFM Copilot
      </Link>
      <nav className="flex items-center gap-1" aria-label="Primary">
        {NAV.map((item) => {
          const active = pathname?.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "px-3 py-1.5 text-sm rounded-sm",
                active
                  ? "text-accent"
                  : "text-text-secondary hover:text-text-primary",
              )}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="ml-auto flex items-center gap-3">
        <SkillPicker />
        <NotificationBell />
        <button
          type="button"
          onClick={onToggleChat}
          className="text-sm text-text-secondary hover:text-text-primary px-3 py-1.5 rounded-sm border border-border-default"
          aria-pressed={chatOpen}
        >
          {chatOpen ? "Hide chat" : "Show chat"}
        </button>
      </div>
    </header>
  );
}
