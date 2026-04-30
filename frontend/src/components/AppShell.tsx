"use client";

import { useEffect, useState } from "react";
import { SkillProvider } from "@/context/SkillContext";
import { TopNav } from "./TopNav";
import { ChatPanel } from "./ChatPanel";

const STORAGE_KEY = "wfm.chat_open";

export function AppShell({ children }: { children: React.ReactNode }) {
  const [chatOpen, setChatOpen] = useState(true);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored !== null) setChatOpen(stored === "1");
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (hydrated) localStorage.setItem(STORAGE_KEY, chatOpen ? "1" : "0");
  }, [chatOpen, hydrated]);

  return (
    <SkillProvider>
      <div className="flex flex-col h-screen w-screen overflow-hidden">
        <TopNav onToggleChat={() => setChatOpen((v) => !v)} chatOpen={chatOpen} />
        <div className="flex flex-1 min-h-0 min-w-0">
          <main className="flex-1 min-w-0 overflow-y-auto">
            <div className="max-w-[1200px] mx-auto px-6 py-6">{children}</div>
          </main>
          {chatOpen ? <ChatPanel /> : null}
        </div>
      </div>
    </SkillProvider>
  );
}
