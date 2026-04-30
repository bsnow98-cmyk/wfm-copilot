import { httpProvider } from "./httpProvider";
import { mockProvider } from "./mockProvider";
import type { ChatProvider } from "./provider";

// When NEXT_PUBLIC_API_URL is set, talk to the real backend. Otherwise the
// mock provider lets the dashboard run with no backend at all (Phase 7 dev).
export const chatProvider: ChatProvider = process.env.NEXT_PUBLIC_API_URL
  ? httpProvider
  : mockProvider;
