// Password sign-in against CyberdyneAuth.
//
// The behaviours here are security-relevant rather than cosmetic: a failure that
// reveals whether an account exists, or a rate limit rendered as a wrong
// password, are both defects even though the screen still "works".

import { beforeEach, describe, expect, it, vi } from "vitest";

// `$lib/app` reads runtime config from `$env/dynamic/public`, which has no value
// outside a SvelteKit server. The URLs are irrelevant here -- fetch is stubbed --
// so an empty env lets the composition layer be tested at all.
vi.mock("$env/dynamic/public", () => ({ env: {} }));

import { NetworkError, RateLimitError } from "$lib/api/client";
import {
  AuthClient,
  CredentialsRejectedError,
  InactiveAccountError,
  InvalidMfaCodeError,
  MfaSessionExpiredError,
} from "$lib/auth/auth-client";
import { beginPasswordLogin, completeMfaLogin } from "$lib/app";
import { session, useStore } from "$lib/auth/session.svelte";
import { describeError } from "$lib/errors";

const TOKENS = { access_token: "acc-1", refresh_token: "ref-1", token_type: "bearer" };

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function client(handler: () => Promise<Response>) {
  const fetchImpl = vi.fn(handler) as unknown as typeof fetch;
  return { auth: new AuthClient("https://auth.example", fetchImpl), fetchImpl };
}

/** A Store that records everything written, so nothing can hide in storage. */
function recordingStore() {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

describe("AuthClient.login", () => {
  it("returns a token pair when CyberdyneAuth answers with tokens", async () => {
    const { auth } = client(async () => json(TOKENS));
    const outcome = await auth.login("ops@example.com", "correct-horse");
    expect(outcome).toEqual({
      kind: "tokens",
      tokens: { access_token: "acc-1", refresh_token: "ref-1" },
    });
  });

  it("returns a challenge when CyberdyneAuth answers mfa_required", async () => {
    const { auth } = client(async () => json({ mfa_required: true, mfa_token: "mfa-1" }));
    const outcome = await auth.login("ops@example.com", "correct-horse");
    expect(outcome).toEqual({ kind: "mfa", mfaToken: "mfa-1" });
  });

  it("posts the credentials to the login endpoint", async () => {
    const { auth, fetchImpl } = client(async () => json(TOKENS));
    await auth.login("ops@example.com", "correct-horse");
    const [url, init] = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("https://auth.example/api/v1/auth/login");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      email: "ops@example.com",
      password: "correct-horse",
    });
  });

  it("does not distinguish a wrong password from an unknown account", async () => {
    // CyberdyneAuth answers 401 for both, and runs a dummy verify so they cost
    // the same. Rendering them differently here would undo that.
    const wrongPassword = client(async () => json({ detail: "Invalid credentials" }, 401));
    const noSuchAccount = client(async () => json({ detail: "Invalid credentials" }, 401));

    const a = await wrongPassword.auth
      .login("real@example.com", "wrong")
      .catch((e: unknown) => e);
    const b = await noSuchAccount.auth
      .login("ghost@example.com", "wrong")
      .catch((e: unknown) => e);

    expect(a).toBeInstanceOf(CredentialsRejectedError);
    expect(b).toBeInstanceOf(CredentialsRejectedError);
    expect(describeError(a)).toBe(describeError(b));
  });

  it("reports rate limiting as rate limiting, not as a wrong password", async () => {
    const { auth } = client(async () =>
      json({ detail: "Too many requests" }, 429, { "Retry-After": "60" }),
    );
    const failure = await auth
      .login("ops@example.com", "correct-horse")
      .catch((e: unknown) => e);

    expect(failure).toBeInstanceOf(RateLimitError);
    expect((failure as RateLimitError).retryAfterSeconds).toBe(60);
    expect(describeError(failure)).toMatch(/too many/i);
    expect(describeError(failure)).not.toMatch(/password/i);
  });

  it("names an inactive account rather than blaming the password", async () => {
    const { auth } = client(async () => json({ detail: "User is inactive" }, 403));
    const failure = await auth
      .login("ops@example.com", "correct-horse")
      .catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(InactiveAccountError);
    expect(describeError(failure)).toMatch(/inactive/i);
  });

  it("attributes an unreachable auth service to CyberdyneAuth, not CyberFS", async () => {
    const { auth } = client(async () => {
      throw new TypeError("failed to fetch");
    });
    const failure = await auth
      .login("ops@example.com", "correct-horse")
      .catch((e: unknown) => e);
    expect(failure).toBeInstanceOf(NetworkError);
    expect((failure as NetworkError).service).toBe("CyberdyneAuth");
    expect(describeError(failure)).not.toContain("CyberFS");
  });

  it("refuses a 200 that carries neither tokens nor a challenge", async () => {
    const { auth } = client(async () => json({ token_type: "bearer" }));
    await expect(auth.login("ops@example.com", "pw")).rejects.toThrow(/usable session/i);
  });
});

describe("AuthClient.verifyMfa", () => {
  it("completes a challenge into a token pair", async () => {
    const { auth, fetchImpl } = client(async () => json(TOKENS));
    const tokens = await auth.verifyMfa("mfa-1", "123456");

    expect(tokens).toEqual({ access_token: "acc-1", refresh_token: "ref-1" });
    const [url, init] = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("https://auth.example/api/v1/auth/mfa/verify");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      mfa_token: "mfa-1",
      code: "123456",
    });
  });

  it("distinguishes a wrong code from an expired challenge", async () => {
    // Both are 401 and differ only by detail, but they need different
    // recoveries: retry the code, versus start the whole sign-in again.
    const badCode = client(async () => json({ detail: "Invalid code" }, 401));
    const expired = client(async () => json({ detail: "MFA session expired" }, 401));

    await expect(badCode.auth.verifyMfa("mfa-1", "000000")).rejects.toBeInstanceOf(
      InvalidMfaCodeError,
    );
    await expect(expired.auth.verifyMfa("mfa-1", "123456")).rejects.toBeInstanceOf(
      MfaSessionExpiredError,
    );
  });

  it("treats an unrecognised 401 as a wrong code, keeping the challenge alive", async () => {
    // If the server ever rewords its detail, staying on the prompt is the safe
    // failure -- restarting a sign-in that was fine is the worse mistake.
    const { auth } = client(async () => json({ detail: "something new" }, 401));
    await expect(auth.verifyMfa("mfa-1", "000000")).rejects.toBeInstanceOf(InvalidMfaCodeError);
  });

  it("surfaces rate limiting on the code endpoint", async () => {
    const { auth } = client(async () => json({ detail: "Too many requests" }, 429));
    await expect(auth.verifyMfa("mfa-1", "000000")).rejects.toBeInstanceOf(RateLimitError);
  });
});

describe("password sign-in composition", () => {
  beforeEach(() => {
    session.accessToken = null;
    session.refreshToken = null;
    session.isAdmin = false;
    session.checked = false;
  });

  it("adopts tokens into the session on success", async () => {
    const store = recordingStore();
    useStore(store);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(TOKENS)),
    );

    const result = await beginPasswordLogin("ops@example.com", "correct-horse", "/quotas");

    expect(result).toEqual({ kind: "signed-in" });
    expect(session.accessToken).toBe("acc-1");
    vi.unstubAllGlobals();
  });

  it("hands back the challenge without adopting a session", async () => {
    const store = recordingStore();
    useStore(store);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ mfa_required: true, mfa_token: "mfa-1" })),
    );

    const result = await beginPasswordLogin("ops@example.com", "correct-horse", "/quotas");

    expect(result).toEqual({ kind: "code-required", mfaToken: "mfa-1" });
    // No session until the second factor is verified.
    expect(session.accessToken).toBeNull();
    vi.unstubAllGlobals();
  });

  it("never writes the password or the code into storage", async () => {
    const store = recordingStore();
    useStore(store);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ mfa_required: true, mfa_token: "mfa-1" })),
    );
    await beginPasswordLogin("ops@example.com", "correct-horse", "/quotas");
    vi.unstubAllGlobals();

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(TOKENS)),
    );
    await completeMfaLogin("mfa-1", "123456");
    vi.unstubAllGlobals();

    const written = [...store.map.entries()].map(([k, v]) => `${k}=${v}`).join("\n");
    expect(written).not.toContain("correct-horse");
    expect(written).not.toContain("123456");
    // The challenge token is a credential too; it belongs in component state.
    expect(written).not.toContain("mfa-1");
  });

  it("remembers the return path so a deep link survives sign-in", async () => {
    const store = recordingStore();
    useStore(store);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(TOKENS)),
    );
    await beginPasswordLogin("ops@example.com", "correct-horse", "/sharing");
    vi.unstubAllGlobals();

    expect(store.getItem("cyberfs.return_to")).toBe("/sharing");
  });
});
