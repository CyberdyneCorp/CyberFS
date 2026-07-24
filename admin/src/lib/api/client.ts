// The single network boundary. Nothing else in the app calls `fetch`, and a
// lint rule stops components from importing this module directly.
//
// CyberFS is a *different origin* from CyberdyneAuth, so cookies are not an
// option: the dashboard holds a bearer token obtained through CyberdyneAuth's
// fragment-mode OAuth flow and sends it on every request.
//
// A 401 triggers one refresh attempt and a replay. Concurrent requests share a
// single in-flight refresh, so a burst of calls after an expiry produces one
// refresh rather than one per call. If the refresh itself fails, the original
// 401 propagates and the layout sends the user to log in again.

import type { ProblemDetail } from "$lib/api/types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly problem: ProblemDetail | null;

  constructor(status: number, problem: ProblemDetail | null, message?: string) {
    super(message ?? problem?.detail ?? `API ${status}`);
    this.status = status;
    this.code = problem?.code ?? "unknown";
    this.problem = problem;
  }
}

/** The caller is authenticated but is not an administrator. */
export class ForbiddenError extends ApiError {
  constructor(problem: ProblemDetail | null) {
    super(403, problem, problem?.detail ?? "administrator privilege is required");
  }
}

/** No usable credential. The layout turns this into a redirect to login. */
export class UnauthorizedError extends ApiError {
  constructor(problem: ProblemDetail | null) {
    super(401, problem, problem?.detail ?? "not signed in");
  }
}

export class RateLimitError extends ApiError {
  readonly retryAfterSeconds: number | null;
  constructor(problem: ProblemDetail | null, retryAfterSeconds: number | null) {
    super(429, problem, "too many requests");
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/** The request never reached the server. Distinct from any HTTP status. */
export class NetworkError extends Error {
  constructor(
    message: string,
    readonly cause?: unknown,
  ) {
    super(message);
  }
}

export interface TokenSource {
  accessToken(): string | null;
  /** Exchange the refresh token for a new pair. False if it cannot be done. */
  refresh(): Promise<boolean>;
  onAuthenticationLost(): void;
}

export interface ClientOptions {
  baseUrl: string;
  tokens: TokenSource;
  fetchImpl?: typeof fetch;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly tokens: TokenSource;
  private readonly doFetch: typeof fetch;
  /** Shared so concurrent 401s cause one refresh, not one each. */
  private refreshing: Promise<boolean> | null = null;

  constructor({ baseUrl, tokens, fetchImpl }: ClientOptions) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.tokens = tokens;
    this.doFetch = fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  async get<T>(path: string, query?: Record<string, unknown>): Promise<T> {
    return this.request<T>("GET", path + encodeQuery(query));
  }

  async put<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("PUT", path, body);
  }

  async post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("POST", path, body);
  }

  async delete<T>(path: string): Promise<T> {
    return this.request<T>("DELETE", path);
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    let response = await this.send(method, path, body);

    if (response.status === 401) {
      const recovered = await this.refreshOnce();
      if (recovered) {
        response = await this.send(method, path, body);
      }
    }

    if (response.ok) {
      return (await readBody(response)) as T;
    }
    throw await toError(response);
  }

  private async send(method: string, path: string, body?: unknown): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = this.tokens.accessToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }

    try {
      return await this.doFetch(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      throw new NetworkError("could not reach CyberFS", cause);
    }
  }

  private async refreshOnce(): Promise<boolean> {
    this.refreshing ??= this.tokens.refresh().finally(() => {
      this.refreshing = null;
    });

    const recovered = await this.refreshing;
    if (!recovered) {
      this.tokens.onAuthenticationLost();
    }
    return recovered;
  }
}

function encodeQuery(query?: Record<string, unknown>): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    // Skip empties so a cleared filter does not become `?actor=`.
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function toError(response: Response): Promise<ApiError> {
  const problem = (await readBody(response)) as ProblemDetail | null;
  switch (response.status) {
    case 401:
      return new UnauthorizedError(problem);
    case 403:
      return new ForbiddenError(problem);
    case 429:
      return new RateLimitError(problem, parseRetryAfter(response));
    default:
      return new ApiError(response.status, problem);
  }
}

function parseRetryAfter(response: Response): number | null {
  const header = response.headers.get("Retry-After");
  if (!header) return null;
  const seconds = Number.parseInt(header, 10);
  return Number.isFinite(seconds) ? seconds : null;
}
