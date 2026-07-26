// Where an operator lands after signing in.
//
// The layout stores the deep link before redirecting to /login, and both
// sign-in paths then report where they are. Without a guard the second write
// clobbers the first, and the operator is sent back to the page they just left.

import { beforeEach, describe, expect, it } from "vitest";

import { rememberReturnPath, takeReturnPath, useStore } from "$lib/auth/session.svelte";

function store() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

describe("return path", () => {
  beforeEach(() => useStore(store()));

  it("keeps the requested route", () => {
    rememberReturnPath("/audit");
    expect(takeReturnPath()).toBe("/audit");
  });

  it("keeps the query string", () => {
    rememberReturnPath("/users?over_quota=true");
    expect(takeReturnPath()).toBe("/users?over_quota=true");
  });

  it("is consumed once", () => {
    rememberReturnPath("/audit");
    takeReturnPath();
    expect(takeReturnPath()).toBe("/");
  });

  it("does not let the login page overwrite a real deep link", () => {
    // Regression: the layout remembered /audit, then the login page remembered
    // its own path, and signing in returned the operator to /login.
    rememberReturnPath("/audit");
    rememberReturnPath("/login");
    expect(takeReturnPath()).toBe("/audit");
  });

  it("does not let the OAuth callback overwrite a real deep link", () => {
    rememberReturnPath("/sharing");
    rememberReturnPath("/auth/callback");
    expect(takeReturnPath()).toBe("/sharing");
  });

  it("never returns a sign-in route, even if one was stored directly", () => {
    // Defence in depth: returning /login here would be a redirect loop.
    useStore({
      getItem: () => "/login",
      setItem: () => {},
      removeItem: () => {},
    });
    expect(takeReturnPath()).toBe("/");
  });

  it("still refuses an off-site path", () => {
    rememberReturnPath("//evil.example/steal");
    expect(takeReturnPath()).toBe("/");
  });
});
