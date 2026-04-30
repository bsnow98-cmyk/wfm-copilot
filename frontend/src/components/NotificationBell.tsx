"use client";

import { useEffect, useRef, useState } from "react";
import { ToolResponseRenderer } from "@/chat/renderers";
import type { ToolResponse } from "@/chat/types";
import {
  fetchNotifications,
  markAllRead,
  markRead,
  type NotificationItem,
} from "@/lib/notifications";

const POLL_INTERVAL_MS = 30_000;

export function NotificationBell() {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Poll on mount + every 30s. Decision in CHAT_WRITE_ACTIONS.md:
  // SSE push can come later if the count metric warrants it.
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const res = await fetchNotifications();
        if (!cancelled) {
          setItems(res.items);
          setUnread(res.unread_count);
        }
      } catch {
        // Silent — the bell shouldn't 500 the page if the API is down.
      }
    }
    tick();
    const id = setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Click-outside-to-close.
  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (!dropdownRef.current?.contains(e.target as Node)) setOpen(false);
    }
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);

  async function handleClickItem(n: NotificationItem) {
    if (n.read_at == null) {
      await markRead(n.id);
      setUnread((u) => Math.max(0, u - 1));
      setItems((prev) =>
        prev.map((it) =>
          it.id === n.id ? { ...it, read_at: new Date().toISOString() } : it,
        ),
      );
    }
  }

  async function handleMarkAll() {
    await markAllRead();
    setUnread(0);
    setItems((prev) =>
      prev.map((it) => ({
        ...it,
        read_at: it.read_at ?? new Date().toISOString(),
      })),
    );
  }

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={`Notifications (${unread} unread)`}
        className="relative text-sm text-text-secondary hover:text-text-primary px-3 py-1.5 rounded-sm border border-border-default"
      >
        Notifications
        {unread > 0 ? (
          <span
            data-mono
            aria-hidden
            className="ml-2 inline-block min-w-[20px] text-center px-1.5 py-0.5 text-xs rounded-sm bg-accent text-white"
          >
            {unread > 99 ? "99+" : unread}
          </span>
        ) : null}
      </button>
      {open ? (
        <div
          role="dialog"
          aria-label="Notifications"
          className="absolute right-0 top-full mt-1 w-[420px] bg-surface border border-border-default rounded-md shadow-none max-h-[60vh] overflow-y-auto z-50"
        >
          <div className="px-4 py-2 border-b border-border-default flex items-center justify-between">
            <span className="text-sm text-text-primary">Notifications</span>
            <button
              type="button"
              onClick={handleMarkAll}
              disabled={unread === 0}
              className="text-xs text-accent hover:text-accent-hover disabled:text-text-muted"
            >
              Mark all read
            </button>
          </div>
          {items.length === 0 ? (
            <div className="px-4 py-8 text-sm text-text-muted text-center">
              No notifications yet.
            </div>
          ) : (
            <ul className="divide-y divide-border-default">
              {items.map((n) => (
                <li
                  key={n.id}
                  onClick={() => handleClickItem(n)}
                  className={
                    "px-4 py-3 cursor-pointer hover:bg-surface-subtle " +
                    (n.read_at == null ? "" : "opacity-60")
                  }
                >
                  <div className="flex items-baseline gap-2 mb-1">
                    <span className="text-xs text-text-muted">
                      {n.category.replace(/_/g, " ")}
                    </span>
                    <span data-mono className="text-xs text-text-muted">
                      {new Date(n.created_at).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  </div>
                  <NotificationPayload payload={n.payload} />
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  );
}

function NotificationPayload({
  payload,
}: {
  payload: NotificationItem["payload"];
}) {
  if (
    payload &&
    typeof payload === "object" &&
    "render" in payload &&
    typeof (payload as { render: unknown }).render === "string"
  ) {
    return <ToolResponseRenderer response={payload as ToolResponse} />;
  }
  return (
    <pre data-mono className="text-xs text-text-muted overflow-x-auto">
      {JSON.stringify(payload, null, 2)}
    </pre>
  );
}
