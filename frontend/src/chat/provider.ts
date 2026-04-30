import type { ToolCall, ToolResponse } from "./types";

export type ChatProviderRequest = {
  conversation_id: string | null;
  message: string;
};

export type ChatHandlers = {
  onToken?: (text: string) => void;
  onToolCall?: (call: ToolCall) => void;
  onToolResult?: (tool: string, result: ToolResponse) => void;
  onError?: (message: string) => void;
  onWarning?: (message: string) => void;
};

export type ChatProviderResult = {
  conversation_id: string;
};

export interface ChatProvider {
  send(req: ChatProviderRequest, handlers: ChatHandlers): Promise<ChatProviderResult>;
}
