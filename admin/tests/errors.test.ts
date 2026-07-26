// Operator-facing error text.
//
// The dashboard spans two origins, so the one thing these messages must get
// right is *which* of them failed.

import { describe, expect, it, vi } from "vitest";

import { NetworkError } from "$lib/api/client";
import { AuthClient } from "$lib/auth/auth-client";
import { describeError } from "$lib/errors";

describe("describeError", () => {
  it("names the service that was unreachable", () => {
    expect(describeError(new NetworkError("CyberFS"))).toContain("Could not reach CyberFS");
    expect(describeError(new NetworkError("CyberdyneAuth"))).toContain(
      "Could not reach CyberdyneAuth",
    );
  });

  it("does not blame CyberFS when CyberdyneAuth is the unreachable one", () => {
    // Regression: every NetworkError used to render as "Could not reach
    // CyberFS", so a rejected call to CyberdyneAuth sent operators to check an
    // API that was healthy the whole time.
    const message = describeError(new NetworkError("CyberdyneAuth"));
    expect(message).not.toContain("CyberFS");
  });

  it("points at the CORS allowlist, since a block reads as unreachable", () => {
    expect(describeError(new NetworkError("CyberdyneAuth"))).toContain("CORS");
  });
});

describe("AuthClient failures", () => {
  it("attributes an unreachable auth service to CyberdyneAuth", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("failed to fetch");
    }) as unknown as typeof fetch;
    const auth = new AuthClient("https://auth.example", fetchImpl);

    // The exact call `beginLogin` makes, which is where the misattribution
    // surfaced: a CORS rejection here was reported as a CyberFS outage.
    const failure = await auth
      .authorizationUrl("google", "https://dash.example/auth/callback")
      .catch((err: unknown) => err);

    expect(failure).toBeInstanceOf(NetworkError);
    expect((failure as NetworkError).service).toBe("CyberdyneAuth");
    expect(describeError(failure)).not.toContain("CyberFS");
  });
});
