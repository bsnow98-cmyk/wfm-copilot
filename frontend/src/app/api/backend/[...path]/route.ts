/**
 * Same-origin proxy to the FastAPI backend.
 *
 * Why this exists: the demo password used to ship to the browser via
 * NEXT_PUBLIC_DEMO_PASSWORD, which Next.js inlines into the public JS bundle —
 * anyone could read it from DevTools and hit /chat directly (burning Anthropic
 * credits). The credential now lives only in this server-side handler.
 *
 * Env (server-side, NOT NEXT_PUBLIC_):
 *   BACKEND_API_URL — FastAPI base URL.  Falls back to NEXT_PUBLIC_API_URL so
 *                     existing local/Vercel configs keep working.
 *   DEMO_PASSWORD   — Basic-auth password. Falls back to
 *                     NEXT_PUBLIC_DEMO_PASSWORD for the same reason (reading it
 *                     here does not inline it into the client bundle; only
 *                     client-code references do that).
 *
 * Responses are streamed through untouched, so the /chat SSE stream works.
 */
import type { NextRequest } from "next/server";

const BACKEND_URL = (
  process.env.BACKEND_API_URL ?? process.env.NEXT_PUBLIC_API_URL
)?.replace(/\/$/, "");
const DEMO_PASSWORD =
  process.env.DEMO_PASSWORD ?? process.env.NEXT_PUBLIC_DEMO_PASSWORD;

type Ctx = { params: Promise<{ path: string[] }> };

async function proxy(req: NextRequest, ctx: Ctx): Promise<Response> {
  if (!BACKEND_URL) {
    return Response.json(
      { detail: "Backend not configured (set BACKEND_API_URL)" },
      { status: 502 },
    );
  }
  const { path } = await ctx.params;
  const url = `${BACKEND_URL}/${path.join("/")}${req.nextUrl.search}`;

  const headers = new Headers();
  for (const name of ["content-type", "accept"]) {
    const v = req.headers.get(name);
    if (v) headers.set(name, v);
  }
  if (DEMO_PASSWORD) {
    headers.set(
      "authorization",
      "Basic " + Buffer.from(`demo:${DEMO_PASSWORD}`).toString("base64"),
    );
  }

  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  const upstream = await fetch(url, {
    method: req.method,
    headers,
    body: hasBody ? req.body : undefined,
    // Node fetch requires duplex for streaming request bodies.
    ...(hasBody ? { duplex: "half" as const } : {}),
    cache: "no-store",
  });

  const respHeaders = new Headers();
  const ct = upstream.headers.get("content-type");
  if (ct) respHeaders.set("content-type", ct);
  return new Response(upstream.body, {
    status: upstream.status,
    headers: respHeaders,
  });
}

export async function GET(req: NextRequest, ctx: Ctx) {
  return proxy(req, ctx);
}
export async function POST(req: NextRequest, ctx: Ctx) {
  return proxy(req, ctx);
}
export async function PATCH(req: NextRequest, ctx: Ctx) {
  return proxy(req, ctx);
}
export async function DELETE(req: NextRequest, ctx: Ctx) {
  return proxy(req, ctx);
}
