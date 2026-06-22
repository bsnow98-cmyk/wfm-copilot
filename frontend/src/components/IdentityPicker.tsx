"use client";

import { useEffect, useState } from "react";
import { PROXY_BASE } from "@/lib/backendProxy";

type UserOut = { username: string; display_name: string; role: string };

const COOKIE = "wfm_user";

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return m ? decodeURIComponent(m[1]) : null;
}

/**
 * RBAC identity picker. The selected username is written to the `wfm_user`
 * cookie; the same-origin proxy forwards it as the Basic-Auth username, and the
 * backend resolves it to a role. Changing identity reloads so every view
 * re-fetches under the new permissions.
 */
export function IdentityPicker() {
  const [users, setUsers] = useState<UserOut[]>([]);
  const [current, setCurrent] = useState<string>("demo");

  useEffect(() => {
    setCurrent(readCookie(COOKIE) ?? "demo");
    fetch(`${PROXY_BASE}/users`)
      .then((r) => (r.ok ? r.json() : []))
      .then((data: UserOut[]) => setUsers(data))
      .catch(() => setUsers([]));
  }, []);

  if (users.length === 0) return null;

  function onChange(username: string) {
    document.cookie = `${COOKIE}=${encodeURIComponent(username)}; path=/; max-age=${60 * 60 * 24 * 30}; samesite=lax`;
    setCurrent(username);
    // Reload so all data + chat re-fetch under the new identity/role.
    window.location.reload();
  }

  return (
    <label className="flex items-center gap-2 text-sm text-text-secondary">
      <span className="text-xs text-text-muted">Acting as</span>
      <select
        value={current}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Switch acting identity"
        className="text-sm border border-border-default rounded-sm px-2 py-1.5 bg-surface focus:outline-none focus:border-accent"
      >
        {users.map((u) => (
          <option key={u.username} value={u.username}>
            {u.display_name} · {u.role}
          </option>
        ))}
      </select>
    </label>
  );
}
