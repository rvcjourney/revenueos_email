import { clearLocalAuthSession, createClient } from "@/lib/supabase/client";

export type BackendHealth = {
  status: "ok";
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly requestId: string | null;
  // The server's structured detail for the error (for example the failed check
  // codes, or the company name found on a website); null when there is none.
  readonly details: Record<string, unknown> | null;

  constructor(
    message: string,
    status: number,
    code: string | null,
    requestId: string | null,
    details: Record<string, unknown> | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export function apiUrl(path: string): string {
  return new URL(path, baseUrl).toString();
}

// Reads (GET/HEAD) are safe to repeat, so a transient failure -- a network blip,
// or the backend answering 500/502/503/504 while the database is briefly busy --
// is retried a couple of times with a short backoff before the user sees an
// error. Writes are never retried here: they would need an idempotency key.
const READ_TIMEOUT_MS = 30_000;
const MAX_READ_RETRIES = 2;
const RETRYABLE_STATUS = new Set([500, 502, 503, 504]);
const NETWORK_ERROR_MESSAGE = "Unable to reach the server. Check your connection and try again.";

function isReadMethod(method: string | undefined): boolean {
  const upper = (method ?? "GET").toUpperCase();
  return upper === "GET" || upper === "HEAD";
}

function retryDelayMs(attempt: number, response: Response | null): number {
  const retryAfterSeconds = Number(response?.headers.get("retry-after"));
  if (Number.isFinite(retryAfterSeconds) && retryAfterSeconds > 0) {
    // A page fires about ten reads at once, and they all fail together when the
    // database is out of connections. A fixed delay would send them back in the same
    // instant and fail them again, so spread them across 0.5x to 1.5x, growing with
    // each attempt.
    const base = Math.min(retryAfterSeconds, 5) * 1000 * (attempt + 1);
    return base * (0.5 + Math.random());
  }
  return 300 * 3 ** attempt + Math.random() * 200;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** One fetch bounded by READ_TIMEOUT_MS, still honouring a caller's own signal. */
async function fetchWithTimeout(url: string, init: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const callerSignal = init.signal ?? null;
  const abortFromCaller = () => controller.abort();
  if (callerSignal?.aborted) controller.abort();
  else callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timer = setTimeout(() => controller.abort(), READ_TIMEOUT_MS);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

async function fetchWithRetry(url: string, init: RequestInit): Promise<Response> {
  const isRead = isReadMethod(init.method);
  const maxRetries = isRead ? MAX_READ_RETRIES : 0;
  for (let attempt = 0; ; attempt += 1) {
    let response: Response | null = null;
    try {
      // Only reads get a timeout: an upload legitimately takes as long as it takes.
      response = isRead ? await fetchWithTimeout(url, init) : await fetch(url, init);
    } catch (error) {
      // The caller cancelled on purpose (e.g. the component unmounted): not a failure.
      if (init.signal?.aborted) throw error;
      if (attempt >= maxRetries) {
        throw new ApiError(NETWORK_ERROR_MESSAGE, 0, "network_error", null);
      }
    }
    if (response && !(RETRYABLE_STATUS.has(response.status) && attempt < maxRetries)) {
      return response;
    }
    await sleep(retryDelayMs(attempt, response));
  }
}

let redirectingToLogin = false;

async function redirectToLogin() {
  if (redirectingToLogin || typeof window === "undefined") return;
  redirectingToLogin = true;
  try {
    await clearLocalAuthSession();
  } catch {
    // If local/session cleanup fails, still leave the protected route. The
    // backend has already rejected the token, so showing authenticated UI
    // would be misleading.
  }
  window.location.href = "/auth/login";
}

async function getAccessToken(): Promise<string | null> {
  const supabase = createClient();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

let refreshInFlight: Promise<string | null> | null = null;

/**
 * Shared across concurrent callers: if several requests 401 at the same
 * moment, they all await the same refresh instead of each calling
 * `refreshSession()` independently, which would risk Supabase's
 * refresh-token rotation invalidating a sibling in-flight refresh.
 */
async function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    const supabase = createClient();
    refreshInFlight = supabase.auth
      .refreshSession()
      .then(({ data, error }) => (error ? null : data.session?.access_token ?? null))
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

type ApiRequestInit = Omit<RequestInit, "headers"> & {
  headers?: Record<string, string>;
  idempotencyKey?: string;
};

/**
 * The one authoritative client the whole app uses to call the backend.
 * Attaches the current Supabase access token as a bearer credential (never
 * a client-supplied user id), retries exactly once through a session
 * refresh on 401, and redirects to login if that refresh also fails --
 * centralized here so no page duplicates token/401 handling. Reads also
 * retry transient failures (see fetchWithRetry).
 */
export async function apiRequest<T>(
  path: string,
  init: ApiRequestInit = {},
): Promise<{ data: T; requestId: string | null }> {
  const { idempotencyKey, headers: extraHeaders, ...rest } = init;

  async function doFetch(accessToken: string | null): Promise<Response> {
    // A FormData body needs the browser to set the multipart boundary itself,
    // so it must not be labelled as JSON.
    const headers: Record<string, string> =
      rest.body instanceof FormData
        ? { ...extraHeaders }
        : { "Content-Type": "application/json", ...extraHeaders };
    if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    return fetchWithRetry(apiUrl(path), { ...rest, headers });
  }

  const token = await getAccessToken();
  let response = await doFetch(token);

  if (response.status === 401) {
    // A missing token right after sign-in can just mean the session is
    // still settling into storage -- give it one fair retry via refresh
    // before treating this as an unrecoverable auth failure.
    const refreshed = await refreshAccessToken();
    response = refreshed ? await doFetch(refreshed) : response;
  }

  const requestId = response.headers.get("x-request-id");

  if (!response.ok) {
    let message = "Request failed";
    let code: string | null = null;
    let details: Record<string, unknown> | null = null;
    try {
      const body = await response.json();
      message = body?.error?.message ?? message;
      code = body?.error?.code ?? null;
      const rawDetails = body?.error?.details;
      details =
        rawDetails && typeof rawDetails === "object" && !Array.isArray(rawDetails)
          ? (rawDetails as Record<string, unknown>)
          : null;
    } catch {
      message = response.statusText || message;
    }
    if (response.status === 401) {
      void redirectToLogin();
    }
    throw new ApiError(message, response.status, code, requestId, details);
  }

  if (response.status === 204) {
    return { data: undefined as T, requestId };
  }
  return { data: (await response.json()) as T, requestId };
}

export async function getBackendHealth() {
  return apiRequest<BackendHealth>("/api/v1/health");
}
