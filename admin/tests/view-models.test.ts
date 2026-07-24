// View models, exercised headlessly against a stub API.
//
// `admin-dashboard/spec.md` requires every view model to be testable without
// mounting a component. Nothing here touches the DOM.

import { describe, expect, it } from "vitest";

import { ForbiddenError, NetworkError } from "$lib/api/client";
import { createAuditVM, toQuery } from "../src/routes/audit/audit.vm.svelte";
import { createHealthVM, overallStatus } from "../src/routes/health/health.vm.svelte";
import { createOverviewVM } from "../src/routes/overview.vm.svelte";
import { createSharingVM, isExpiringSoon } from "../src/routes/sharing/sharing.vm.svelte";
import {
  createUserDetailVM,
  parseQuota,
} from "../src/routes/users/[userId]/user-detail.vm.svelte";
import { createUsersVM, isInactive } from "../src/routes/users/users.vm.svelte";
import { aLink, aTenant, aUser, anOperations, createStubApi } from "./stub-api";

// --- overview --------------------------------------------------------------

describe("overview view model", () => {
  it("starts empty and not loading", () => {
    const vm = createOverviewVM(createStubApi());
    expect(vm.hasData).toBe(false);
    expect(vm.state.loading).toBe(false);
  });

  it("loads the tenant summary", async () => {
    const vm = createOverviewVM(createStubApi());
    await vm.load();
    expect(vm.hasData).toBe(true);
    expect(vm.state.data?.file_count).toBe(10);
  });

  it("defaults to a 30 day window", async () => {
    const api = createStubApi();
    await createOverviewVM(api).load();
    expect(api.calls).toContain("overview:30");
  });

  it("reloads when the window changes", async () => {
    const api = createStubApi();
    const vm = createOverviewVM(api);
    await vm.load();
    await vm.setWindow(7);
    expect(api.calls).toEqual(["overview:30", "overview:7"]);
  });

  it("does not reload when the window is unchanged", async () => {
    const api = createStubApi();
    const vm = createOverviewVM(api);
    await vm.load();
    await vm.setWindow(30);
    expect(api.calls).toHaveLength(1);
  });

  it("surfaces a failure as a readable message", async () => {
    const api = createStubApi();
    api.failWith = new NetworkError("down");
    const vm = createOverviewVM(api);
    await vm.load();
    expect(vm.state.error).toContain("Could not reach CyberFS");
    expect(vm.state.loading).toBe(false);
  });

  it("keeps the previous data when a refresh fails", async () => {
    // A failed refresh should not blank the page the operator was reading.
    const api = createStubApi();
    const vm = createOverviewVM(api);
    await vm.load();
    api.failWith = new NetworkError("down");
    await vm.load();
    expect(vm.state.data).not.toBeNull();
    expect(vm.state.error).not.toBeNull();
  });

  it("never scales growth bars by zero", async () => {
    // An all-quiet window must still render, not divide by zero.
    const api = createStubApi({ overview: async () => aTenant({ growth: [] }) });
    const vm = createOverviewVM(api);
    await vm.load();
    expect(vm.maxGrowthBytes).toBeGreaterThan(0);
  });
});

// --- users -----------------------------------------------------------------

describe("users view model", () => {
  it("loads users", async () => {
    const vm = createUsersVM(createStubApi());
    await vm.load();
    expect(vm.visible).toHaveLength(1);
  });

  it("asks the API to sort, since it can sort the whole set", async () => {
    const api = createStubApi();
    const vm = createUsersVM(api);
    await vm.load();
    await vm.setSort("files");
    expect(api.calls).toContain("users:files:false");
  });

  it("asks the API to filter over-quota", async () => {
    const api = createStubApi();
    const vm = createUsersVM(api);
    await vm.toggleOverQuota();
    expect(api.calls).toContain("users:used:true");
  });

  it("filters by search locally, without a round trip", async () => {
    const api = createStubApi({
      users: async () => ({ items: [aUser({ subject: "alice" }), aUser({ subject: "bob" })] }),
    });
    const vm = createUsersVM(api);
    await vm.load();
    const before = api.calls.length;

    vm.setSearch("ali");

    expect(vm.visible.map((u) => u.subject)).toEqual(["alice"]);
    expect(api.calls).toHaveLength(before);
  });

  it("search is case insensitive", async () => {
    const api = createStubApi({
      users: async () => ({ items: [aUser({ subject: "Alice" })] }),
    });
    const vm = createUsersVM(api);
    await vm.load();
    vm.setSearch("ALICE");
    expect(vm.visible).toHaveLength(1);
  });

  it("filters inactive users locally", async () => {
    const now = new Date("2026-07-24T00:00:00Z");
    const api = createStubApi({
      users: async () => ({
        items: [
          aUser({ subject: "recent", last_seen_at: "2026-07-23T00:00:00Z" }),
          aUser({ subject: "stale", last_seen_at: "2026-01-01T00:00:00Z" }),
        ],
      }),
    });
    const vm = createUsersVM(api, () => now);
    await vm.load();

    vm.toggleInactive();

    expect(vm.visible.map((u) => u.subject)).toEqual(["stale"]);
  });

  it("counts over-quota users regardless of the active filter", async () => {
    const api = createStubApi({
      users: async () => ({ items: [aUser({ over_quota: true }), aUser()] }),
    });
    const vm = createUsersVM(api);
    await vm.load();
    expect(vm.overQuotaCount).toBe(1);
  });

  it("reports whether anything is filtered", async () => {
    const vm = createUsersVM(createStubApi());
    await vm.load();
    expect(vm.isFiltered).toBe(false);
    vm.setSearch("x");
    expect(vm.isFiltered).toBe(true);
  });

  it("clearing filters reloads only when the server-side filter was on", async () => {
    const api = createStubApi();
    const vm = createUsersVM(api);
    await vm.load();
    vm.setSearch("x");
    await vm.clearFilters();
    expect(api.calls).toHaveLength(1);

    await vm.toggleOverQuota();
    await vm.clearFilters();
    expect(api.calls).toHaveLength(3);
  });

  it("surfaces a failure", async () => {
    const api = createStubApi();
    api.failWith = new ForbiddenError(null);
    const vm = createUsersVM(api);
    await vm.load();
    expect(vm.state.error).toContain("administrator access");
  });
});

describe("isInactive", () => {
  const now = new Date("2026-07-24T00:00:00Z");

  it("treats a user who has never been seen as inactive", () => {
    expect(isInactive(aUser({ last_seen_at: null }), now)).toBe(true);
  });

  it("treats a recent visit as active", () => {
    expect(isInactive(aUser({ last_seen_at: "2026-07-20T00:00:00Z" }), now)).toBe(false);
  });

  it("treats an unreadable timestamp as inactive rather than crashing", () => {
    expect(isInactive(aUser({ last_seen_at: "nonsense" }), now)).toBe(true);
  });
});

// --- user detail -----------------------------------------------------------

describe("parseQuota", () => {
  it("accepts a plain byte count", () => {
    expect(parseQuota("1024")).toBe(1024);
  });

  it("accepts a unit suffix, with or without a space", () => {
    expect(parseQuota("1 GB")).toBe(1024 ** 3);
    expect(parseQuota("2gb")).toBe(2 * 1024 ** 3);
    expect(parseQuota("1.5 MB")).toBe(Math.round(1.5 * 1024 ** 2));
  });

  it("tolerates thousands separators", () => {
    expect(parseQuota("1,048,576")).toBe(1_048_576);
  });

  it("rejects what it cannot read, rather than guessing", () => {
    // Silently setting the wrong quota would be worse than refusing.
    expect(parseQuota("")).toBeNull();
    expect(parseQuota("lots")).toBeNull();
    expect(parseQuota("-5")).toBeNull();
    expect(parseQuota("10 parsecs")).toBeNull();
  });

  it("accepts zero, which means no storage at all", () => {
    expect(parseQuota("0")).toBe(0);
  });
});

describe("user detail view model", () => {
  it("loads a user and seeds the quota field", async () => {
    const vm = createUserDetailVM(createStubApi(), "u-1");
    await vm.load();
    expect(vm.state.user?.user_id).toBe("u-1");
    expect(vm.state.quotaInput).toBe("1000");
  });

  it("breaks usage into live, trash, and versions", async () => {
    const vm = createUserDetailVM(createStubApi(), "u-1");
    await vm.load();
    expect(vm.breakdown.map((slice) => slice.label)).toEqual(["Live", "Trash", "Versions"]);
    expect(vm.breakdown[0].bytes).toBe(80);
  });

  it("the breakdown shares sum to one hundred", async () => {
    const vm = createUserDetailVM(createStubApi(), "u-1");
    await vm.load();
    const total = vm.breakdown.reduce((sum, slice) => sum + slice.share, 0);
    expect(total).toBeCloseTo(100, 5);
  });

  it("has an empty breakdown before loading", () => {
    expect(createUserDetailVM(createStubApi(), "u-1").breakdown).toEqual([]);
  });

  it("saves a parsed quota", async () => {
    const api = createStubApi();
    const vm = createUserDetailVM(api, "u-1");
    await vm.load();
    vm.setQuotaInput("2 GB");
    await vm.saveQuota();
    expect(api.calls).toContain(`setQuota:u-1:${2 * 1024 ** 3}`);
    expect(vm.state.saved).toBe(true);
  });

  it("refuses to save an unparseable quota", async () => {
    const api = createStubApi();
    const vm = createUserDetailVM(api, "u-1");
    await vm.load();
    vm.setQuotaInput("plenty");
    await vm.saveQuota();
    expect(api.calls).not.toContain("setQuota:u-1:0");
    expect(vm.state.quotaError).toContain("Enter a size");
  });

  it("cannot save while the input is invalid", async () => {
    const vm = createUserDetailVM(createStubApi(), "u-1");
    await vm.load();
    vm.setQuotaInput("nonsense");
    expect(vm.canSave).toBe(false);
    vm.setQuotaInput("1 GB");
    expect(vm.canSave).toBe(true);
  });

  it("reflects the recomputed figures after a save", async () => {
    // Lowering a quota below usage should immediately show over-quota.
    const api = createStubApi({
      setQuota: async (userId, quotaBytes) =>
        aUser({ user_id: userId, quota_bytes: quotaBytes, over_quota: true }),
    });
    const vm = createUserDetailVM(api, "u-1");
    await vm.load();
    vm.setQuotaInput("1");
    await vm.saveQuota();
    expect(vm.state.user?.over_quota).toBe(true);
  });

  it("clears the saved flag when the field is edited again", async () => {
    const vm = createUserDetailVM(createStubApi(), "u-1");
    await vm.load();
    vm.setQuotaInput("1 GB");
    await vm.saveQuota();
    vm.setQuotaInput("2 GB");
    expect(vm.state.saved).toBe(false);
  });
});

// --- sharing ---------------------------------------------------------------

describe("sharing view model", () => {
  it("loads links", async () => {
    const vm = createSharingVM(createStubApi());
    await vm.load();
    expect(vm.state.links).toHaveLength(1);
  });

  it("counts links with no passphrase", async () => {
    const api = createStubApi({
      links: async () => ({
        items: [aLink({ passphrase_protected: false }), aLink({ passphrase_protected: true })],
      }),
    });
    const vm = createSharingVM(api);
    await vm.load();
    expect(vm.unprotectedCount).toBe(1);
  });

  it("requires confirmation before revoking", async () => {
    const api = createStubApi();
    const vm = createSharingVM(api);
    await vm.load();

    vm.askRevoke("l-1");

    expect(vm.state.pendingRevoke).toBe("l-1");
    expect(api.calls).not.toContain("revokeLink:l-1");
  });

  it("revokes once confirmed and removes it from the list", async () => {
    const api = createStubApi();
    const vm = createSharingVM(api);
    await vm.load();
    vm.askRevoke("l-1");

    await vm.confirmRevoke();

    expect(api.calls).toContain("revokeLink:l-1");
    expect(vm.state.links).toHaveLength(0);
    expect(vm.state.notice).toContain("immediately");
  });

  it("cancelling leaves the link alone", async () => {
    const api = createStubApi();
    const vm = createSharingVM(api);
    await vm.load();
    vm.askRevoke("l-1");
    vm.cancelRevoke();
    await vm.confirmRevoke();
    expect(api.calls).not.toContain("revokeLink:l-1");
    expect(vm.state.links).toHaveLength(1);
  });

  it("keeps the link listed when revocation fails", async () => {
    const api = createStubApi();
    const vm = createSharingVM(api);
    await vm.load();
    api.failWith = new NetworkError("down");
    vm.askRevoke("l-1");

    await vm.confirmRevoke();

    expect(vm.state.links).toHaveLength(1);
    expect(vm.state.error).not.toBeNull();
  });
});

describe("isExpiringSoon", () => {
  const now = new Date("2026-07-24T00:00:00Z");

  it("is false for a link that never expires", () => {
    expect(isExpiringSoon(aLink({ expires_at: null }), now)).toBe(false);
  });

  it("is true within the next week", () => {
    expect(isExpiringSoon(aLink({ expires_at: "2026-07-27T00:00:00Z" }), now)).toBe(true);
  });

  it("is false once already expired", () => {
    expect(isExpiringSoon(aLink({ expires_at: "2026-07-01T00:00:00Z" }), now)).toBe(false);
  });
});

// --- audit -----------------------------------------------------------------

describe("audit query building", () => {
  const empty = { actor: "", action: "", target: "", since: "", until: "" };

  it("omits blank filters so a cleared field does not narrow the query", () => {
    expect(toQuery(empty)).toEqual({ limit: 100 });
  });

  it("includes filters that are set", () => {
    const query = toQuery({ ...empty, actor: "alice", action: "grant.created" });
    expect(query.actor).toBe("alice");
    expect(query.action).toBe("grant.created");
  });

  it("trims whitespace-only filters away", () => {
    expect(toQuery({ ...empty, actor: "   " }).actor).toBeUndefined();
  });

  it("carries the cursor when paging", () => {
    expect(toQuery(empty, "cur-1").cursor).toBe("cur-1");
  });
});

describe("audit view model", () => {
  it("loads a first page", async () => {
    const api = createStubApi({
      audit: async () => ({ items: [{ action: "a" } as never], next_cursor: "cur-1" }),
    });
    const vm = createAuditVM(api);
    await vm.load();
    expect(vm.state.entries).toHaveLength(1);
    expect(vm.hasMore).toBe(true);
  });

  it("appends rather than replacing when loading more", async () => {
    let call = 0;
    const api = createStubApi({
      audit: async () => {
        call += 1;
        return call === 1
          ? { items: [{ action: "first" } as never], next_cursor: "cur-1" }
          : { items: [{ action: "second" } as never], next_cursor: null };
      },
    });
    const vm = createAuditVM(api);
    await vm.load();
    await vm.loadMore();

    expect(vm.state.entries.map((e) => e.action)).toEqual(["first", "second"]);
    expect(vm.hasMore).toBe(false);
  });

  it("will not append the same page twice on a double click", async () => {
    const api = createStubApi({
      audit: async () => ({ items: [{ action: "x" } as never], next_cursor: "cur-1" }),
    });
    const vm = createAuditVM(api);
    await vm.load();

    await Promise.all([vm.loadMore(), vm.loadMore()]);

    expect(vm.state.entries).toHaveLength(2);
  });

  it("does nothing when there is no next page", async () => {
    const api = createStubApi();
    const vm = createAuditVM(api);
    await vm.load();
    const before = api.calls.length;
    await vm.loadMore();
    expect(api.calls).toHaveLength(before);
  });

  it("applying a filter restarts from the first page", async () => {
    // No cursor in the query: a filtered view must not resume mid-list.
    const api = createStubApi();
    const vm = createAuditVM(api);
    await vm.load();
    vm.setFilter("actor", "alice");
    await vm.applyFilters();

    expect(api.calls.at(-1)).toBe("audit::alice");
  });

  it("clearing filters empties them all", async () => {
    const vm = createAuditVM(createStubApi());
    vm.setFilter("actor", "alice");
    vm.setFilter("action", "grant.created");
    await vm.clearFilters();
    expect(vm.isFiltered).toBe(false);
  });

  it("reports emptiness only once loading has finished", async () => {
    const vm = createAuditVM(createStubApi());
    await vm.load();
    expect(vm.isEmpty).toBe(true);
  });

  it("clears stale entries when a load fails", async () => {
    const api = createStubApi();
    const vm = createAuditVM(api);
    await vm.load();
    api.failWith = new NetworkError("down");
    await vm.load();
    expect(vm.state.entries).toEqual([]);
    expect(vm.state.error).not.toBeNull();
  });
});

// --- health ----------------------------------------------------------------

describe("overallStatus", () => {
  it("is unknown before anything is known", () => {
    expect(overallStatus([])).toBe("unknown");
  });

  it("is healthy when everything is up", () => {
    expect(overallStatus(anOperations().components)).toBe("healthy");
  });

  it("is degraded when only an optional dependency is down", () => {
    // Redis down means slower, never wrong -- the page must not show red.
    const components = anOperations().components.map((c) =>
      c.name === "cache" ? { ...c, status: "down" as const } : c,
    );
    expect(overallStatus(components)).toBe("degraded");
  });

  it("is unhealthy when a required dependency is down", () => {
    const components = anOperations().components.map((c) =>
      c.name === "postgres" ? { ...c, status: "down" as const } : c,
    );
    expect(overallStatus(components)).toBe("unhealthy");
  });

  it("a required failure outranks an optional one", () => {
    const down = new Set(["minio", "cache"]);
    const components = anOperations().components.map((c) =>
      down.has(c.name) ? { ...c, status: "down" as const } : c,
    );
    expect(overallStatus(components)).toBe("unhealthy");
  });
});

describe("health view model", () => {
  it("loads operations", async () => {
    const vm = createHealthVM(createStubApi());
    await vm.load();
    expect(vm.overall).toBe("healthy");
    expect(vm.cacheAvailable).toBe(true);
  });

  it("separates degraded from failing", async () => {
    const api = createStubApi({
      operations: async () =>
        anOperations({
          components: [
            {
              name: "postgres",
              status: "up",
              criticality: "required",
              latency_ms: 1,
              detail: null,
            },
            {
              name: "cache",
              status: "down",
              criticality: "optional",
              latency_ms: null,
              detail: "unreachable",
            },
          ],
        }),
    });
    const vm = createHealthVM(api);
    await vm.load();

    expect(vm.failing).toHaveLength(0);
    expect(vm.degraded.map((c) => c.name)).toEqual(["cache"]);
    expect(vm.overall).toBe("degraded");
  });

  it("surfaces jobs that have never run", async () => {
    const vm = createHealthVM(createStubApi());
    await vm.load();
    expect(vm.staleJobs.map((j) => j.name)).toEqual(["backup"]);
  });

  it("purges a dataset and refreshes", async () => {
    const api = createStubApi();
    const vm = createHealthVM(api);
    await vm.load();

    await vm.purge("perm");

    expect(api.calls).toContain("purgeCache:perm");
    expect(vm.state.notice).toContain("3 perm");
    // Refreshed so the key count reflects the purge.
    expect(api.calls.filter((c) => c === "operations")).toHaveLength(2);
  });

  it("reports a purge failure", async () => {
    const api = createStubApi();
    const vm = createHealthVM(api);
    await vm.load();
    api.failWith = new NetworkError("down");

    await vm.purge("perm");

    expect(vm.state.error).not.toBeNull();
    expect(vm.state.purging).toBeNull();
  });
});
