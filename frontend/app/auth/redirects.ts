import type { NextRequest } from "next/server";

/**
 * `next` is never used verbatim as a redirect target: it must be an internal
 * path, never an absolute/protocol-relative URL, to avoid turning an auth
 * link into an open redirect.
 */
export function safeNextPath(next: string | null): string {
  if (!next) return "/app";
  if (!next.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
    return "/app";
  }
  return next;
}

/**
 * The origin the browser used. Behind a reverse proxy the handler's own URL
 * can be the container's (http://0.0.0.0:3000), and redirecting there strands
 * the user; the proxy passes the public host in X-Forwarded-Host. The
 * container's port is bound to localhost only, so nothing but the proxy can
 * set it.
 */
export function publicOrigin(request: NextRequest): string {
  const forwardedHost = request.headers.get("x-forwarded-host");
  if (forwardedHost && /^[a-z0-9.-]+(:\d+)?$/i.test(forwardedHost)) {
    const proto = request.headers.get("x-forwarded-proto") === "http" ? "http" : "https";
    return `${proto}://${forwardedHost}`;
  }
  return new URL(request.url).origin;
}

/** Where a link that could not be verified sends the user. */
export function failedLinkUrl(request: NextRequest): URL {
  const url = new URL("/auth/login", publicOrigin(request));
  url.searchParams.set("error", "auth_callback_failed");
  return url;
}
