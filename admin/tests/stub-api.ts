// A stub API for the view-model tests.
//
// View models depend on the `AdminApi` interface, never on the client, so this
// is all a headless test needs -- no DOM, no server, no network.

import type { AdminApi } from "$lib/api/endpoints";
import type {
  AuditPage,
  AuditQuery,
  LinkList,
  OperationsSummary,
  PublicLink,
  PurgeResponse,
  TenantSummary,
  UserQuery,
  UserStorage,
  UserStorageList,
} from "$lib/api/types";

export function aUser(overrides: Partial<UserStorage> = {}): UserStorage {
  return {
    user_id: "u-1",
    subject: "alice",
    quota_bytes: 1000,
    used_bytes: 100,
    live_bytes: 80,
    trashed_bytes: 15,
    version_bytes: 5,
    percent_used: 10,
    over_quota: false,
    file_count: 3,
    folder_count: 2,
    encrypted_file_count: 1,
    encrypted_bytes: 40,
    encrypted_share: 50,
    grants_given: 1,
    grants_received: 0,
    is_admin: false,
    created_at: "2026-01-01T00:00:00Z",
    last_seen_at: "2026-07-24T00:00:00Z",
    ...overrides,
  };
}

export function aTenant(overrides: Partial<TenantSummary> = {}): TenantSummary {
  return {
    total_bytes: 500,
    live_bytes: 400,
    trashed_bytes: 100,
    version_bytes: 20,
    file_count: 10,
    folder_count: 4,
    user_count: 2,
    active_user_count: 1,
    encrypted_file_count: 3,
    encrypted_bytes: 120,
    encrypted_share: 30,
    public_link_count: 2,
    grant_count: 5,
    content_types: [{ content_type: "application/pdf", file_count: 4, bytes: 300 }],
    growth: [{ day: "2026-07-20", files_added: 2, bytes_added: 100 }],
    top_consumers: [aUser()],
    ...overrides,
  };
}

export function aLink(overrides: Partial<PublicLink> = {}): PublicLink {
  return {
    id: "l-1",
    node_id: "n-1",
    created_by: "alice",
    created_at: "2026-07-01T00:00:00Z",
    expires_at: null,
    revoked: false,
    passphrase_protected: false,
    access_count: 4,
    last_accessed_at: "2026-07-20T00:00:00Z",
    ...overrides,
  };
}

export function anOperations(overrides: Partial<OperationsSummary> = {}): OperationsSummary {
  return {
    components: [
      { name: "postgres", status: "up", criticality: "required", latency_ms: 2, detail: null },
      { name: "minio", status: "up", criticality: "required", latency_ms: 5, detail: null },
      { name: "cache", status: "up", criticality: "optional", latency_ms: 1, detail: null },
    ],
    jobs: [
      {
        name: "purge",
        last_run_at: "2026-07-24T00:00:00Z",
        outcome: "success",
        duration_seconds: 1.5,
        detail: null,
        has_run: true,
      },
      {
        name: "backup",
        last_run_at: null,
        outcome: null,
        duration_seconds: null,
        detail: null,
        has_run: false,
      },
    ],
    cache: { available: true, keys: 12 },
    totals_reconcile: true,
    ...overrides,
  };
}

export interface StubApi extends AdminApi {
  calls: string[];
  failWith: Error | null;
}

export function createStubApi(overrides: Partial<AdminApi> = {}): StubApi {
  const calls: string[] = [];
  const stub = {
    calls,
    failWith: null as Error | null,

    async overview(growthDays: number): Promise<TenantSummary> {
      calls.push(`overview:${growthDays}`);
      if (stub.failWith) throw stub.failWith;
      return aTenant();
    },

    async users(query: UserQuery = {}): Promise<UserStorageList> {
      calls.push(`users:${query.sort_by ?? ""}:${query.over_quota ?? false}`);
      if (stub.failWith) throw stub.failWith;
      return { items: [aUser()] };
    },

    async user(userId: string): Promise<UserStorage> {
      calls.push(`user:${userId}`);
      if (stub.failWith) throw stub.failWith;
      return aUser({ user_id: userId });
    },

    async setQuota(userId: string, quotaBytes: number): Promise<UserStorage> {
      calls.push(`setQuota:${userId}:${quotaBytes}`);
      if (stub.failWith) throw stub.failWith;
      return aUser({ user_id: userId, quota_bytes: quotaBytes });
    },

    async links(): Promise<LinkList> {
      calls.push("links");
      if (stub.failWith) throw stub.failWith;
      return { items: [aLink()] };
    },

    async revokeLink(linkId: string): Promise<void> {
      calls.push(`revokeLink:${linkId}`);
      if (stub.failWith) throw stub.failWith;
    },

    async audit(query: AuditQuery = {}): Promise<AuditPage> {
      calls.push(`audit:${query.cursor ?? ""}:${query.actor ?? ""}`);
      if (stub.failWith) throw stub.failWith;
      return { items: [], next_cursor: null };
    },

    async operations(): Promise<OperationsSummary> {
      calls.push("operations");
      if (stub.failWith) throw stub.failWith;
      return anOperations();
    },

    async purgeCache(dataset: string): Promise<PurgeResponse> {
      calls.push(`purgeCache:${dataset}`);
      if (stub.failWith) throw stub.failWith;
      return { dataset, keys_removed: 3 };
    },

    ...overrides,
  };
  return stub as StubApi;
}
