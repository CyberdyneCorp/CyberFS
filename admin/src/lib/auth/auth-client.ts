// The second network boundary: CyberdyneAuth.
//
// Two ways in, one destination. The OAuth flow hands the browser to
// CyberdyneAuth and comes back with tokens in the URL fragment; password
// sign-in exchanges credentials for the same token pair here. Everything after
// `adoptTokens()` is identical, and `is_admin` still comes from the profile
// endpoint -- the dashboard never decides that for itself.

import { NetworkError, RateLimitError } from "$lib/api/client";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
}

export interface Profile {
  id: string;
  email: string | null;
  is_admin: boolean;
}

/**
 * What `POST /auth/login` answered.
 *
 * CyberdyneAuth returns either a token pair or a second-factor challenge, and
 * says which by setting `mfa_required` explicitly -- so branch on that field
 * rather than inferring from whether an `access_token` happens to be present.
 */
export type LoginOutcome =
  { kind: "tokens"; tokens: TokenPair } | { kind: "mfa"; mfaToken: string };

/**
 * Sign-in was refused.
 *
 * Deliberately carries no hint about *why*: CyberdyneAuth answers 401 for both
 * an unknown account and a wrong password, and the dashboard must not undo that
 * by rendering them differently.
 */
export class CredentialsRejectedError extends Error {
  constructor() {
    super("That email and password combination is not correct.");
  }
}

/** The account exists but has been deactivated. */
export class InactiveAccountError extends Error {
  constructor() {
    super("This account is inactive. Ask an administrator to re-enable it.");
  }
}

/** The second-factor challenge is no longer valid; sign-in must start over. */
export class MfaSessionExpiredError extends Error {
  constructor() {
    super("That sign-in attempt expired. Enter your email and password again.");
  }
}

/** The one-time code was wrong. The challenge is still usable. */
export class InvalidMfaCodeError extends Error {
  constructor() {
    super("That code is not correct. Check your authenticator app and try again.");
  }
}

export class AuthClient {
  private readonly baseUrl: string;
  private readonly doFetch: typeof fetch;

  constructor(baseUrl: string, fetchImpl?: typeof fetch) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    // Resolved per call rather than bound once, so a later replacement of
    // `globalThis.fetch` is honoured. `app.ts` memoises its wiring, so a client
    // built early would otherwise hold the fetch that existed at that moment.
    this.doFetch = fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  }

  /**
   * Starts fragment-mode OAuth and returns where to send the browser.
   *
   * `frontend_redirect` is bound to the state row server-side, so it cannot be
   * swapped after consent. If it is not on CyberdyneAuth's allowlist this fails
   * here, before the user has consented to anything.
   */
  async authorizationUrl(provider: string, frontendRedirect: string): Promise<string> {
    const query = new URLSearchParams({
      return_mode: "fragment",
      frontend_redirect: frontendRedirect,
    });
    const body = await this.send(`/api/v1/auth/oauth/${provider}?${query}`, { method: "GET" });
    const url = (body as { authorization_url?: string }).authorization_url;
    if (!url) throw new Error("CyberdyneAuth did not return an authorization URL.");
    return url;
  }

  /** Exchanges credentials for tokens, or for a second-factor challenge. */
  async login(email: string, password: string): Promise<LoginOutcome> {
    const response = await this.fetchJson("/api/v1/auth/login", { email, password });
    if (!response.ok) throw loginError(response);

    const body = parse(response.text) as {
      mfa_required?: boolean;
      mfa_token?: string;
    } & TokenPair;
    if (body.mfa_required === true && body.mfa_token) {
      return { kind: "mfa", mfaToken: body.mfa_token };
    }
    return { kind: "tokens", tokens: requireTokens(body) };
  }

  /** Completes a second-factor challenge. */
  async verifyMfa(mfaToken: string, code: string): Promise<TokenPair> {
    const response = await this.fetchJson("/api/v1/auth/mfa/verify", {
      mfa_token: mfaToken,
      code,
    });
    if (!response.ok) throw mfaError(response);
    return requireTokens(parse(response.text) as TokenPair);
  }

  async refresh(refreshToken: string): Promise<TokenPair> {
    return (await this.send("/api/v1/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })) as TokenPair;
  }

  async profile(accessToken: string): Promise<Profile> {
    const body = (await this.send("/api/v1/users/me", {
      method: "GET",
      headers: { Authorization: `Bearer ${accessToken}` },
    })) as { id: string; email: string | null; is_admin?: boolean };
    return { id: body.id, email: body.email ?? null, is_admin: body.is_admin === true };
  }

  /** POSTs JSON and reports the outcome without throwing on an HTTP status. */
  private async fetchJson(path: string, body: unknown): Promise<Answer> {
    const response = await this.raw(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return {
      ok: response.ok,
      status: response.status,
      text: await response.text(),
      retryAfter: parseRetryAfter(response),
    };
  }

  private async raw(path: string, init: RequestInit): Promise<Response> {
    try {
      return await this.doFetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: { Accept: "application/json", ...init.headers },
      });
    } catch (cause) {
      throw new NetworkError("CyberdyneAuth", cause);
    }
  }

  private async send(path: string, init: RequestInit): Promise<unknown> {
    const response = await this.raw(path, init);
    const text = await response.text();
    if (!response.ok) {
      throw new Error(detailOf(text) ?? `CyberdyneAuth returned ${response.status}.`);
    }
    return parse(text);
  }
}

interface Answer {
  ok: boolean;
  status: number;
  text: string;
  retryAfter: number | null;
}

function loginError(answer: Answer): Error {
  if (answer.status === 429) return new RateLimitError(null, answer.retryAfter);
  if (answer.status === 401) return new CredentialsRejectedError();
  if (answer.status === 403) return new InactiveAccountError();
  // A 422 means the address is not a well-formed one. Telling the operator
  // their credentials were refused is both true and no more revealing.
  if (answer.status === 422) return new CredentialsRejectedError();
  return new Error(detailOf(answer.text) ?? `CyberdyneAuth returned ${answer.status}.`);
}

function mfaError(answer: Answer): Error {
  if (answer.status === 429) return new RateLimitError(null, answer.retryAfter);
  if (answer.status === 401) {
    // Both cases are 401 and differ only by detail. Match the expiry wording,
    // but fall back to "wrong code" -- if the server ever rewords this, keeping
    // the operator on the prompt is the safe failure, since restarting a
    // sign-in that was actually fine is the more disruptive mistake.
    const detail = detailOf(answer.text) ?? "";
    return /expired/i.test(detail) ? new MfaSessionExpiredError() : new InvalidMfaCodeError();
  }
  return new Error(detailOf(answer.text) ?? `CyberdyneAuth returned ${answer.status}.`);
}

function requireTokens(body: TokenPair): TokenPair {
  if (!body?.access_token || !body?.refresh_token) {
    throw new Error("CyberdyneAuth did not return a usable session.");
  }
  return { access_token: body.access_token, refresh_token: body.refresh_token };
}

function parse(text: string): unknown {
  return text ? JSON.parse(text) : null;
}

function parseRetryAfter(response: Response): number | null {
  const header = response.headers.get("Retry-After");
  if (!header) return null;
  const seconds = Number.parseInt(header, 10);
  return Number.isFinite(seconds) ? seconds : null;
}

/** FastAPI errors are `{"detail": "..."}`; anything else is not worth showing raw. */
function detailOf(text: string): string | null {
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    return typeof parsed.detail === "string" ? parsed.detail : null;
  } catch {
    return null;
  }
}
