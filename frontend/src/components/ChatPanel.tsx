"use client";

import { useEffect, useRef, useState } from "react";
import { chatProvider } from "@/chat";
import { loadConversation } from "@/chat/loadConversation";
import { ToolResponseRenderer } from "@/chat/renderers";
import type { ChatTurn, ToolCall, ToolResponse } from "@/chat/types";

const SUGGESTED = [
  "Show today's forecast for sales_inbound",
  "What does the schedule look like today?",
  "Compare 80/20 vs 90/15 scenarios",
];

const newId = () => `m_${Math.random().toString(36).slice(2, 10)}`;

export function ChatPanel() {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Mount: pull a stored conversation_id and rehydrate turns from the server.
  useEffect(() => {
    const stored = localStorage.getItem("wfm.conversation_id");
    if (!stored) return;
    setConversationId(stored);
    let cancelled = false;
    loadConversation(stored)
      .then((restored) => {
        if (cancelled) return;
        if (restored && restored.length > 0) setTurns(restored);
      })
      .catch(() => {
        // Don't block the UI on a failed reload — the user can still chat,
        // they just won't see prior turns. The next persistence error will
        // surface its own warning.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (conversationId)
      localStorage.setItem("wfm.conversation_id", conversationId);
  }, [conversationId]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [turns, pending]);

  async function send(message: string) {
    if (!message.trim() || pending) return;
    setInput("");
    setPending(true);

    const userTurn: ChatTurn = {
      role: "user",
      id: newId(),
      content: message,
      created_at: new Date().toISOString(),
    };
    const assistantId = newId();
    const assistantTurn: ChatTurn = {
      role: "assistant",
      id: assistantId,
      content: "",
      created_at: new Date().toISOString(),
      tool_calls: [],
      tool_results: [],
    };
    setTurns((prev) => [...prev, userTurn, assistantTurn]);

    const updateAssistant = (
      mut: (t: Extract<ChatTurn, { role: "assistant" }>) => void,
    ) => {
      setTurns((prev) =>
        prev.map((t) => {
          if (t.id !== assistantId || t.role !== "assistant") return t;
          const copy: Extract<ChatTurn, { role: "assistant" }> = {
            ...t,
            tool_calls: [...(t.tool_calls ?? [])],
            tool_results: [...(t.tool_results ?? [])],
          };
          mut(copy);
          return copy;
        }),
      );
    };

    try {
      const res = await chatProvider.send(
        { conversation_id: conversationId, message },
        {
          onToken: (text: string) =>
            updateAssistant((t) => {
              t.content += text;
            }),
          onToolCall: (call: ToolCall) =>
            updateAssistant((t) => {
              t.tool_calls = [...(t.tool_calls ?? []), call];
            }),
          onToolResult: (_tool: string, result: ToolResponse) =>
            updateAssistant((t) => {
              t.tool_results = [...(t.tool_results ?? []), result];
            }),
          onError: (msg: string) =>
            updateAssistant((t) => {
              t.tool_results = [
                ...(t.tool_results ?? []),
                { render: "error", message: msg },
              ];
            }),
          onWarning: (msg: string) => setWarning(msg),
        },
      );
      setConversationId(res.conversation_id || null);
    } finally {
      setPending(false);
    }
  }

  return (
    <aside
      className="w-[420px] shrink-0 border-l border-border-default bg-surface flex flex-col"
      aria-label="Chat copilot"
    >
      {warning ? (
        <PersistenceWarning
          message={warning}
          onDismiss={() => setWarning(null)}
        />
      ) : null}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-4 py-3 space-y-4"
        aria-live="polite"
      >
        {turns.length === 0 ? (
          <EmptyState onPick={send} />
        ) : (
          turns.map((t) => <Turn key={t.id} turn={t} />)
        )}
        {pending ? <StreamingDots /> : null}
      </div>
      <form
        className="border-t border-border-default p-3"
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
      >
        <textarea
          ref={inputRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(input);
            }
          }}
          placeholder="Ask why your forecast looks the way it does."
          rows={2}
          className="w-full resize-none border border-border-default rounded-sm px-3 py-2 text-sm focus:outline-none focus:border-accent"
        />
        <div className="mt-2 flex items-center justify-between text-xs text-text-muted">
          <span data-mono>⌘K to focus · Enter to send</span>
          <button
            type="submit"
            disabled={pending || !input.trim()}
            className="text-sm px-3 py-1.5 rounded-sm bg-accent text-white disabled:bg-border-strong"
          >
            Send
          </button>
        </div>
      </form>
    </aside>
  );
}

function EmptyState({ onPick }: { onPick: (s: string) => void }) {
  return (
    <div className="text-sm text-text-secondary">
      <p className="mb-3">Ask why your forecast looks the way it does.</p>
      <ul className="space-y-2">
        {SUGGESTED.map((s) => (
          <li key={s}>
            <button
              type="button"
              onClick={() => onPick(s)}
              className="text-left w-full border border-border-default rounded-sm px-3 py-2 hover:border-border-strong"
            >
              {s}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Turn({ turn }: { turn: ChatTurn }) {
  if (turn.role === "user") {
    return (
      <div className="text-sm">
        <div className="text-text-muted text-xs mb-1">You</div>
        <div className="text-text-primary whitespace-pre-wrap">
          {turn.content}
        </div>
      </div>
    );
  }
  return (
    <div className="text-sm space-y-2">
      <div className="text-text-muted text-xs">Assistant</div>
      {turn.content ? (
        <div className="text-text-primary whitespace-pre-wrap">
          {turn.content}
        </div>
      ) : null}
      {turn.tool_results?.map((r, i) => (
        <ToolResponseRenderer key={i} response={r} />
      ))}
    </div>
  );
}

function PersistenceWarning({
  message,
  onDismiss,
}: {
  message: string;
  onDismiss: () => void;
}) {
  return (
    <div
      role="status"
      className="border-b border-severity-medium/40 bg-severity-medium/5 px-4 py-2 text-xs text-text-primary flex items-start gap-3"
    >
      <span className="flex-1">{message}</span>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="text-accent hover:text-accent-hover underline-offset-2 hover:underline"
      >
        Refresh
      </button>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss warning"
        className="text-text-muted hover:text-text-primary"
      >
        ×
      </button>
    </div>
  );
}

function StreamingDots() {
  return (
    <div className="flex gap-1" aria-label="Assistant is responding">
      <span className="w-1.5 h-1.5 rounded-full bg-text-muted animate-pulse" />
      <span className="w-1.5 h-1.5 rounded-full bg-text-muted animate-pulse [animation-delay:120ms]" />
      <span className="w-1.5 h-1.5 rounded-full bg-text-muted animate-pulse [animation-delay:240ms]" />
    </div>
  );
}
