"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { ALL_SKILLS, isSkillKey, type SkillFilter } from "@/lib/skills";
import { FALLBACK_SKILLS, fetchSkills, type Skill } from "@/lib/fetchSkills";

const STORAGE_KEY = "wfm.skill_filter";

type Ctx = {
  skill: SkillFilter;
  setSkill: (s: SkillFilter) => void;
  /** Live skill list. Populated from `GET /skills` when the API is reachable;
   *  falls back to the hardcoded SKILLS array otherwise. Always non-empty. */
  skills: Skill[];
  /** True until the first fetchSkills resolves (success or fallback). */
  skillsLoading: boolean;
};

const SkillContext = createContext<Ctx | null>(null);

export function SkillProvider({ children }: { children: React.ReactNode }) {
  // Default to ALL_SKILLS — the safe view that doesn't hide data.
  const [skill, setSkillState] = useState<SkillFilter>(ALL_SKILLS);
  const [hydrated, setHydrated] = useState(false);
  const [skills, setSkills] = useState<Skill[]>(FALLBACK_SKILLS);
  const [skillsLoading, setSkillsLoading] = useState(true);

  useEffect(() => {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === ALL_SKILLS || (stored && isSkillKey(stored))) {
      setSkillState(stored as SkillFilter);
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (hydrated) localStorage.setItem(STORAGE_KEY, skill);
  }, [skill, hydrated]);

  // Fetch the live skill list once on mount. fetchSkills returns the
  // FALLBACK_SKILLS on any failure mode, so this always resolves to a
  // non-empty list and never throws.
  useEffect(() => {
    let cancelled = false;
    fetchSkills().then((list) => {
      if (cancelled) return;
      setSkills(list);
      setSkillsLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const setSkill = useCallback((s: SkillFilter) => setSkillState(s), []);

  return (
    <SkillContext.Provider value={{ skill, setSkill, skills, skillsLoading }}>
      {children}
    </SkillContext.Provider>
  );
}

export function useSkill(): Ctx {
  const ctx = useContext(SkillContext);
  if (!ctx) {
    throw new Error("useSkill must be used inside <SkillProvider>");
  }
  return ctx;
}
