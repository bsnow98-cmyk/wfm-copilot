import { fireEvent, render, screen, act } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { SkillProvider, useSkill } from "@/context/SkillContext";
import { ALL_SKILLS } from "@/lib/skills";

function Probe() {
  const { skill, setSkill } = useSkill();
  return (
    <div>
      <span data-testid="current">{skill}</span>
      <button onClick={() => setSkill("sales")}>pick sales</button>
      <button onClick={() => setSkill(ALL_SKILLS)}>pick all</button>
    </div>
  );
}

describe("SkillContext", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("defaults to ALL_SKILLS when nothing is stored", () => {
    render(
      <SkillProvider>
        <Probe />
      </SkillProvider>,
    );
    expect(screen.getByTestId("current").textContent).toBe(ALL_SKILLS);
  });

  it("rehydrates a stored skill on mount", async () => {
    localStorage.setItem("wfm.skill_filter", "support");
    render(
      <SkillProvider>
        <Probe />
      </SkillProvider>,
    );
    // Mount effect runs synchronously in vitest; no waitFor needed.
    expect(screen.getByTestId("current").textContent).toBe("support");
  });

  it("setSkill updates state and persists to localStorage", async () => {
    render(
      <SkillProvider>
        <Probe />
      </SkillProvider>,
    );
    act(() => {
      fireEvent.click(screen.getByText("pick sales"));
    });
    expect(screen.getByTestId("current").textContent).toBe("sales");
    expect(localStorage.getItem("wfm.skill_filter")).toBe("sales");

    act(() => {
      fireEvent.click(screen.getByText("pick all"));
    });
    expect(localStorage.getItem("wfm.skill_filter")).toBe(ALL_SKILLS);
  });

  it("ignores invalid stored values and falls back to ALL_SKILLS", () => {
    localStorage.setItem("wfm.skill_filter", "not-a-skill");
    render(
      <SkillProvider>
        <Probe />
      </SkillProvider>,
    );
    expect(screen.getByTestId("current").textContent).toBe(ALL_SKILLS);
  });

  it("throws a clear error when used outside SkillProvider", () => {
    // Suppress React's error boundary console noise for this one.
    const orig = console.error;
    console.error = () => {};
    expect(() => render(<Probe />)).toThrow(/SkillProvider/);
    console.error = orig;
  });
});
