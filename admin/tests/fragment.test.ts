import { describe, expect, it } from "vitest";

import { parseOAuthError, parseOAuthFragment, safeReturnPath } from "$lib/auth/fragment";

describe("parseOAuthFragment", () => {
  it("reads a complete fragment", () => {
    const parsed = parseOAuthFragment(
      "#access_token=abc&refresh_token=def&token_type=Bearer&expires_in=900&is_new_user=true",
    );
    expect(parsed).toEqual({
      accessToken: "abc",
      refreshToken: "def",
      tokenType: "Bearer",
      expiresIn: 900,
      isNewUser: true,
    });
  });

  it("refuses a fragment missing either token", () => {
    // A half-populated fragment is a broken handshake, not a partial session.
    expect(parseOAuthFragment("#access_token=abc")).toBeNull();
    expect(parseOAuthFragment("#refresh_token=def")).toBeNull();
  });

  it("ignores anything that is not a fragment", () => {
    expect(parseOAuthFragment("?access_token=abc&refresh_token=def")).toBeNull();
    expect(parseOAuthFragment("")).toBeNull();
  });

  it("defaults a missing expiry rather than producing NaN", () => {
    const parsed = parseOAuthFragment("#access_token=a&refresh_token=b");
    expect(parsed?.expiresIn).toBe(0);
  });
});

describe("parseOAuthError", () => {
  it("prefers the description", () => {
    expect(parseOAuthError("#error=access_denied&error_description=User+said+no")).toBe(
      "User said no",
    );
  });

  it("falls back to the code", () => {
    expect(parseOAuthError("#error=access_denied")).toBe("access_denied");
  });

  it("is null when there is no error", () => {
    expect(parseOAuthError("#access_token=a")).toBeNull();
  });
});

describe("safeReturnPath", () => {
  it("keeps a same-site path", () => {
    expect(safeReturnPath("/users/u-1")).toBe("/users/u-1");
  });

  it("refuses an absolute URL", () => {
    // Otherwise login becomes an open redirect.
    expect(safeReturnPath("https://evil.example/steal")).toBe("/");
  });

  it("refuses a protocol-relative URL", () => {
    expect(safeReturnPath("//evil.example/steal")).toBe("/");
  });

  it("falls back when there is nothing saved", () => {
    expect(safeReturnPath(null)).toBe("/");
  });
});
