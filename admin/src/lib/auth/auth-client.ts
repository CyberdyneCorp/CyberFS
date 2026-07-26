// The second network boundary: CyberdyneAuth.
//
// Only three calls are needed. Starting the OAuth handshake, exchanging a
// refresh token, and reading the profile -- the last of which is where
// `is_admin` comes from, since the dashboard must not decide that for itself.

import { NetworkError } from "$lib/api/client";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
}

export interface Profile {
  id: string;
  email: string | null;
  is_admin: boolean;
}

export class AuthClient {
  private readonly baseUrl: string;
  private readonly doFetch: typeof fetch;

  constructor(baseUrl: string, fetchImpl?: typeof fetch) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.doFetch = fetchImpl ?? globalThis.fetch.bind(globalThis);
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

  private async send(path: string, init: RequestInit): Promise<unknown> {
    let response: Response;
    try {
      response = await this.doFetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: { Accept: "application/json", ...init.headers },
      });
    } catch (cause) {
      throw new NetworkError("CyberdyneAuth", cause);
    }

    const text = await response.text();
    if (!response.ok) {
      throw new Error(detailOf(text) ?? `CyberdyneAuth returned ${response.status}.`);
    }
    return text ? JSON.parse(text) : null;
  }
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
