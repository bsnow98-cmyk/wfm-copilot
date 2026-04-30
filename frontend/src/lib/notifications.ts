import type { ToolResponse } from "@/chat/types";

const API_BASE = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
const DEMO_PASSWORD = process.env.NEXT_PUBLIC_DEMO_PASSWORD;

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

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = {};
  if (DEMO_PASSWORD) h.Authorization = "Basic " + btoa(`demo:${DEMO_PASSWORD}`);
  return h;
}

export async function fetchNotifications(): Promise<NotificationsResponse> {
  if (!API_BASE) {
    return { items: [], unread_count: 0 };
  }
  const res = await fetch(`${API_BASE}/notifications?limit=20`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`fetchNotifications ${res.status}`);
  return (await res.json()) as NotificationsResponse;
}

export async function markRead(id: string): Promise<void> {
  if (!API_BASE) return;
  await fetch(`${API_BASE}/notifications/${id}/read`, {
    method: "POST",
    headers: authHeaders(),
  });
}

export async function markAllRead(): Promise<void> {
  if (!API_BASE) return;
  await fetch(`${API_BASE}/notifications/read-all`, {
    method: "POST",
    headers: authHeaders(),
  });
}
