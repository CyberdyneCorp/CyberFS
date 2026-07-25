// The network boundary: token attachment, error mapping, and the 401 refresh.

import { describe, expect, it, vi } from "vitest";

import {
  ApiClient,
  ApiError,
  ForbiddenError,
  NetworkError,
  RateLimitError,
  UnauthorizedError,
  type TokenSource,
} from "$lib/api/client";

interface StubTokens extends TokenSource {
  token: string | null;
  refreshes: number;
  refreshSucceeds: boolean;
  lost: number;
}

function createTokens(overrides: Partial<StubTokens> = {}): StubTokens {
  const tokens: StubTokens = {
    token: "tok-1",
    refreshes: 0,
    refreshSucceeds: true,
    lost: 0,
    accessToken: () => tokens.token,
    async refresh() {
      tokens.refreshes += 1;
      if (tokens.refreshSucceeds) tokens.token = "tok-2";
      return tokens.refreshSucceeds;
    },
    onAuthenticationLost() {
      tokens.lost += 1;
    },
    ...overrides,
  };
  return tokens;
}

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/** Typed so `mock.calls[n]` gives back the URL and init the client actually sent. */
function stubFetch(handler: (url: string, init: RequestInit) => Promise<Response>) {
  return vi.fn((url: string, init: RequestInit) => handler(url, init));
}

function createClient(
  fetchImpl: ReturnType<typeof stubFetch>,
  tokens: TokenSource = createTokens(),
) {
  return new ApiClient({
    baseUrl: "https://api.example.com/",
    tokens,
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });
}

describe("request shaping", () => {
  it("attaches the bearer token", async () => {
    const fetchImpl = stubFetch(async () => jsonResponse(200, { ok: true }));
    await createClient(fetchImpl).get("/admin/users");

    const [, init] = fetchImpl.mock.calls[0];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok-1");
  });

  it("sends no Authorization header when there is no token", async () => {
    const fetchImpl = stubFetch(async () => jsonResponse(200, {}));
    const tokens = createTokens({ accessToken: () => null });
    await createClient(fetchImpl, tokens).get("/admin/users");

    const [, init] = fetchImpl.mock.calls[0];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("strips the trailing slash from the base URL", async () => {
    const fetchImpl = stubFetch(async () => jsonResponse(200, {}));
    await createClient(fetchImpl).get("/admin/users");
    expect(fetchImpl.mock.calls[0][0]).toBe("https://api.example.com/admin/users");
  });

  it("omits empty query values so a cleared filter is not sent", async () => {
    const fetchImpl = stubFetch(async () => jsonResponse(200, {}));
    await createClient(fetchImpl).get("/admin/audit", {
      actor: "alice",
      action: "",
      target: undefined,
      cursor: null,
    });
    expect(fetchImpl.mock.calls[0][0]).toBe("https://api.example.com/admin/audit?actor=alice");
  });

  it("sends a JSON body on PUT", async () => {
    const fetchImpl = stubFetch(async () => jsonResponse(200, {}));
    await createClient(fetchImpl).put("/admin/users/u-1/quota", {
      quota_bytes: 10,
    });

    const [, init] = fetchImpl.mock.calls[0];
    expect(init.body).toBe('{"quota_bytes":10}');
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("returns null for a 204", async () => {
    const fetchImpl = stubFetch(async () => new Response(null, { status: 204 }));
    const result = await createClient(fetchImpl).delete("/admin/links/l-1");
    expect(result).toBeNull();
  });
});

describe("error mapping", () => {
  const cases = [
    { status: 403, type: ForbiddenError },
    { status: 401, type: UnauthorizedError },
    { status: 429, type: RateLimitError },
    { status: 500, type: ApiError },
  ] as const;

  for (const { status, type } of cases) {
    it(`maps ${status} to ${type.name}`, async () => {
      // 401 must not be retried forever: refresh fails, so the error surfaces.
      const tokens = createTokens({ refreshSucceeds: false });
      const fetchImpl = stubFetch(async () =>
        jsonResponse(status, { detail: "nope", code: "x" }),
      );
      const client = createClient(fetchImpl, tokens);

      await expect(client.get("/admin/users")).rejects.toBeInstanceOf(type);
    });
  }

  it("carries the problem detail through", async () => {
    const fetchImpl = stubFetch(async () =>
      jsonResponse(409, { detail: "name already taken", code: "conflict" }),
    );
    const client = createClient(fetchImpl);

    await expect(client.get("/admin/users")).rejects.toMatchObject({
      status: 409,
      code: "conflict",
      message: "name already taken",
    });
  });

  it("reads Retry-After off a 429", async () => {
    const tokens = createTokens();
    const fetchImpl = stubFetch(async () => jsonResponse(429, {}, { "Retry-After": "42" }));
    const client = createClient(fetchImpl, tokens);

    await expect(client.get("/admin/users")).rejects.toMatchObject({ retryAfterSeconds: 42 });
  });

  it("tolerates an error body that is not JSON", async () => {
    const fetchImpl = stubFetch(async () => new Response("<html>502</html>", { status: 502 }));
    const client = createClient(fetchImpl);
    await expect(client.get("/admin/users")).rejects.toBeInstanceOf(ApiError);
  });

  it("distinguishes an unreachable server from any HTTP status", async () => {
    const fetchImpl = stubFetch(async () => {
      throw new TypeError("failed to fetch");
    });
    const client = createClient(fetchImpl);
    await expect(client.get("/admin/users")).rejects.toBeInstanceOf(NetworkError);
  });
});

describe("401 handling", () => {
  it("refreshes once and replays the request", async () => {
    const tokens = createTokens();
    let call = 0;
    const fetchImpl = stubFetch(async () => {
      call += 1;
      return call === 1 ? jsonResponse(401, {}) : jsonResponse(200, { ok: true });
    });

    const result = await createClient(fetchImpl, tokens).get<{
      ok: boolean;
    }>("/admin/users");

    expect(result.ok).toBe(true);
    expect(tokens.refreshes).toBe(1);
    expect(tokens.lost).toBe(0);
  });

  it("replays with the new token, not the expired one", async () => {
    const tokens = createTokens();
    let call = 0;
    const fetchImpl = stubFetch(async () => {
      call += 1;
      return call === 1 ? jsonResponse(401, {}) : jsonResponse(200, {});
    });

    await createClient(fetchImpl, tokens).get("/admin/users");

    const [, init] = fetchImpl.mock.calls[1];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok-2");
  });

  it("does not retry a second time when the replay is also rejected", async () => {
    const tokens = createTokens();
    const fetchImpl = stubFetch(async () => jsonResponse(401, {}));
    const client = createClient(fetchImpl, tokens);

    await expect(client.get("/admin/users")).rejects.toBeInstanceOf(UnauthorizedError);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(tokens.refreshes).toBe(1);
  });

  it("reports the session lost when the refresh fails", async () => {
    const tokens = createTokens({ refreshSucceeds: false });
    const fetchImpl = stubFetch(async () => jsonResponse(401, {}));
    const client = createClient(fetchImpl, tokens);

    await expect(client.get("/admin/users")).rejects.toBeInstanceOf(UnauthorizedError);
    expect(tokens.lost).toBe(1);
    // The request is not replayed when there is nothing better to send.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("a burst of expired requests causes one refresh, not one each", async () => {
    // Every panel on a page loads at once; without single-flight each would
    // spend its own refresh token and all but one would fail.
    const tokens = createTokens();
    const seen = new Set<string>();
    const fetchImpl = stubFetch(async (url: string) => {
      if (!seen.has(url)) {
        seen.add(url);
        return jsonResponse(401, {});
      }
      return jsonResponse(200, { ok: true });
    });
    const client = createClient(fetchImpl, tokens);

    await Promise.all([client.get("/a"), client.get("/b"), client.get("/c")]);

    expect(tokens.refreshes).toBe(1);
  });

  it("refreshes again on a later expiry", async () => {
    // The in-flight promise must be cleared, or the second expiry would reuse
    // a settled refresh and replay with a token that is already stale.
    const tokens = createTokens();
    let call = 0;
    const fetchImpl = stubFetch(async () => {
      call += 1;
      return call % 2 === 1 ? jsonResponse(401, {}) : jsonResponse(200, {});
    });
    const client = createClient(fetchImpl, tokens);

    await client.get("/a");
    await client.get("/b");

    expect(tokens.refreshes).toBe(2);
  });
});
