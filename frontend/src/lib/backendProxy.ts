/**
 * All browser → backend calls go through the same-origin /api/backend proxy
 * (src/app/api/backend/[...path]/route.ts), which attaches the Basic-auth
 * credential server-side. The browser bundle never holds the password.
 *
 * NEXT_PUBLIC_API_URL stays as the "a backend exists" flag — it is just a URL,
 * not a secret — so pages can keep choosing live data vs synthetic fallback.
 */
export const HAS_BACKEND = Boolean(process.env.NEXT_PUBLIC_API_URL);
export const PROXY_BASE = "/api/backend";
