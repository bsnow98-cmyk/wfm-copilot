import type { ChatTurn, ToolCall, ToolResponse } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
const DEMO_PASSWORD = process.env.NEXT_PUBLIC_DEMO_PASSWORD;

type ServerMessage = {
  id: string;
  role: "user" | "assistant" | "tool_result";
  content: unknown;
  tool_calls: unknown;
  created_at: string;
};

/**
 * Fetch a saved conversation and reconstruct the turns the chat panel renders.
 *
 * Returns null if there's no API base configured (mock-provider mode) or if
 * the server has no record of this id (e.g. user has a stale localStorage
 * entry from a wiped DB). Throws on network errors so the caller can decide
 * whether to surface them.
 */
export async function loadConversation(
  conversationId: string,
): Promise<ChatTurn[] | null> {
  if (!API_BASE) return null;

  const headers: Record<string, string> = {};
  if (DEMO_PASSWORD) headers.Authorization = "Basic " + btoa(`demo:${DEMO_PASSWORD}`);

  const res = await fetch(
    `${API_BASE}/chat/conversations/${conversationId}`,
    { headers },
  );
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`load conversation ${res.status}`);

  const data = (await res.json()) as { messages: ServerMessage[] };
  return reconstructTurns(data.messages);
}

/**
 * Server stores three roles (user / assistant / tool_result). The chat panel
 * renders two (user / assistant). Fold tool_result rows into the preceding
 * assistant turn so inline renders show up in the right place.
 */
export function reconstructTurns(messages: ServerMessage[]): ChatTurn[] {
  const turns: ChatTurn[] = [];

  for (const m of messages) {
    if (m.role === "user") {
      const content = typeof m.content === "string" ? m.content : JSON.stringify(m.content);
      turns.push({
        role: "user",
        id: m.id,
        content,
        created_at: m.created_at,
      });
    } else if (m.role === "assistant") {
      const blocks = Array.isArray(m.tool_calls) ? m.tool_calls : [];
      const text = extractText(blocks, m.content);
      const tool_calls: ToolCall[] = [];
      for (const b of blocks as Array<Record<string, unknown>>) {
        if (b.type === "tool_use") {
          tool_calls.push({
            tool_name: b.name as ToolCall["tool_name"],
            arguments: (b.input as Record<string, unknown>) ?? {},
          });
        }
      }
      turns.push({
        role: "assistant",
        id: m.id,
        content: text,
        created_at: m.created_at,
        tool_calls,
        tool_results: [],
      });
    } else if (m.role === "tool_result") {
      // The latest assistant turn owns the renders. Re-parse the server-side
      // JSON-stringified payload back into ToolResponse shape.
      const last = turns[turns.length - 1];
      if (!last || last.role !== "assistant") continue;
      const items = Array.isArray(m.content) ? m.content : [];
      for (const it of items as Array<Record<string, unknown>>) {
        const raw = it.content;
        const parsed = typeof raw === "string" ? safeParse(raw) : raw;
        if (parsed && typeof parsed === "object" && "render" in parsed) {
          last.tool_results = [...(last.tool_results ?? []), parsed as ToolResponse];
        }
      }
    }
  }
  return turns;
}

function extractText(blocks: unknown, fallback: unknown): string {
  if (Array.isArray(blocks)) {
    const parts: string[] = [];
    for (const b of blocks as Array<Record<string, unknown>>) {
      if (b.type === "text" && typeof b.text === "string") parts.push(b.text);
    }
    if (parts.length) return parts.join("");
  }
  if (typeof fallback === "string") return fallback;
  return "";
}

function safeParse(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}
