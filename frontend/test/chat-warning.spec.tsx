import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Mock both modules ChatPanel imports from before importing the panel itself.
// The mock provider invokes onWarning so we can assert the banner shows up.
vi.mock("@/chat", () => ({
  chatProvider: {
    send: vi.fn(async (_req: unknown, handlers: { onWarning?: (m: string) => void }) => {
      handlers.onWarning?.("Couldn't save part of this conversation.");
      return { conversation_id: "c1" };
    }),
  },
}));

vi.mock("@/chat/loadConversation", () => ({
  loadConversation: vi.fn(async () => null),
}));

import { ChatPanel } from "@/components/ChatPanel";

describe("ChatPanel persistence warning", () => {
  it("shows the banner when the provider emits onWarning, and dismisses it", async () => {
    render(<ChatPanel />);

    // Suggested-prompt chips are the easiest way to fire send().
    const chip = screen.getByText("Show today's forecast for sales_inbound");
    fireEvent.click(chip);

    const banner = await waitFor(() =>
      screen.getByRole("status", { name: undefined }),
    );
    expect(banner.textContent).toContain("Couldn't save part of this conversation");

    // Refresh button is present (we don't actually click it — jsdom can't
    // navigate). The dismiss button should remove the banner.
    expect(screen.getByText("Refresh")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Dismiss warning"));

    await waitFor(() => {
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    });
  });
});
