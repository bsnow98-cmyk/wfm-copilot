import type { ChatHandlers, ChatProvider, ChatProviderRequest, ChatProviderResult } from "./provider";
import type { ToolCall, ToolResponse } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
const CHAT_URL = API_BASE ? `${API_BASE}/chat` : "/api/chat";

const DEMO_PASSWORD = process.env.NEXT_PUBLIC_DEMO_PASSWORD;

type SSEEvent =
  | { type: "token"; text: string }
  | { type: "tool_call"; tool: ToolCall["tool_name"]; args: Record<string, unknown> }
  | { type: "tool_result"; tool: string; result: ToolResponse }
  | { type: "done"; conversation_id: string }
  | { type: "error"; message: string }
  | { type: "persistence_warning"; role: string; message: string };

export const httpProvider: ChatProvider = {
  async send(req: ChatProviderRequest, handlers: ChatHandlers): Promise<ChatProviderResult> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    };
    if (DEMO_PASSWORD) {
      headers.Authorization = "Basic " + btoa(`demo:${DEMO_PASSWORD}`);
    }

    const res = await fetch(CHAT_URL, {
      method: "POST",
      headers,
      body: JSON.stringify({
        conversation_id: req.conversation_id,
        message: req.message,
      }),
    });

    if (!res.ok || !res.body) {
      const text = await safeText(res);
      const message = `Chat request failed (${res.status}). ${text}`.trim();
      handlers.onError?.(message);
      throw new Error(message);
    }

    const conversationFromHeader = res.headers.get("x-conversation-id");
    let conversationId = conversationFromHeader ?? req.conversation_id ?? "";

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);

        const dataLines = frame
          .split("\n")
          .filter((l) => l.startsWith("data: "))
          .map((l) => l.slice(6));
        if (dataLines.length === 0) continue;
        const payload = dataLines.join("\n");

        let event: SSEEvent;
        try {
          event = JSON.parse(payload) as SSEEvent;
        } catch {
          continue;
        }

        switch (event.type) {
          case "token":
            handlers.onToken?.(event.text);
            break;
          case "tool_call":
            handlers.onToolCall?.({
              tool_name: event.tool,
              arguments: event.args,
            });
            break;
          case "tool_result":
            handlers.onToolResult?.(event.tool, event.result);
            break;
          case "done":
            conversationId = event.conversation_id || conversationId;
            break;
          case "error":
            handlers.onError?.(event.message);
            break;
          case "persistence_warning":
            handlers.onWarning?.(event.message);
            break;
        }
      }
    }

    return { conversation_id: conversationId };
  },
};

async function safeText(res: Response): Promise<string> {
  try {
    return await res.text();
  } catch {
    return "";
  }
}
