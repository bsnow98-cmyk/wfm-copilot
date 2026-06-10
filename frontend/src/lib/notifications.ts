import type { ToolResponse } from "@/chat/types";
import { HAS_BACKEND, PROXY_BASE } from "@/lib/backendProxy";

export type NotificationItem = {
  id: string;
  created_at: string;
  read_at: string | null;
  category: string;
  source: string;
  conversation_id: string | null;
  // Server stores the payload using the same render contract as chat tools.
  // We type it as ToolResponse so the existing renderer registry works
  // without a separate notification renderer (decision D-3 reuse).
  payload: ToolResponse | Record<string, unknown>;
};

export type NotificationsResponse = {
  items: NotificationItem[];
  unread_count: number;
};

export async function fetchNotifications(): Promise<NotificationsResponse> {
  if (!HAS_BACKEND) {
    return { items: [], unread_count: 0 };
  }
  const res = await fetch(`${PROXY_BASE}/notifications?limit=20`);
  if (!res.ok) throw new Error(`fetchNotifications ${res.status}`);
  return (await res.json()) as NotificationsResponse;
}

export async function markRead(id: string): Promise<void> {
  if (!HAS_BACKEND) return;
  await fetch(`${PROXY_BASE}/notifications/${id}/read`, {
    method: "POST",
  });
}

export async function markAllRead(): Promise<void> {
  if (!HAS_BACKEND) return;
  await fetch(`${PROXY_BASE}/notifications/read-all`, {
    method: "POST",
  });
}
