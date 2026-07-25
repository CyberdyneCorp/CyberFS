// Typed calls onto the admin API. View models depend on this interface, so a
// test can substitute a stub without a DOM, a server, or a network.

import type { ApiClient } from "$lib/api/client";
import type {
  AuditPage,
  AuditQuery,
  LinkList,
  OperationsSummary,
  PurgeResponse,
  TenantSummary,
  UserQuery,
  UserStorage,
  UserStorageList,
} from "$lib/api/types";

export interface AdminApi {
  overview(growthDays: number, topN?: number): Promise<TenantSummary>;
  users(query?: UserQuery): Promise<UserStorageList>;
  user(userId: string): Promise<UserStorage>;
  setQuota(userId: string, quotaBytes: number): Promise<UserStorage>;
  links(limit?: number, cursor?: string): Promise<LinkList>;
  revokeLink(linkId: string): Promise<void>;
  audit(query?: AuditQuery): Promise<AuditPage>;
  operations(): Promise<OperationsSummary>;
  purgeCache(dataset: string): Promise<PurgeResponse>;
}

export function createAdminApi(client: ApiClient): AdminApi {
  return {
    overview: (growthDays, topN = 10) =>
      client.get<TenantSummary>("/api/v1/admin/overview", {
        growth_days: growthDays,
        top_n: topN,
      }),

    users: (query = {}) => client.get<UserStorageList>("/api/v1/admin/users", { ...query }),

    user: (userId) => client.get<UserStorage>(`/api/v1/admin/users/${userId}`),

    setQuota: (userId, quotaBytes) =>
      client.put<UserStorage>(`/api/v1/admin/users/${userId}/quota`, {
        quota_bytes: quotaBytes,
      }),

    links: (limit = 100, cursor) =>
      client.get<LinkList>("/api/v1/admin/links", { limit, cursor }),

    revokeLink: (linkId) => client.delete<void>(`/api/v1/admin/links/${linkId}`),

    audit: (query = {}) => client.get<AuditPage>("/api/v1/admin/audit", { ...query }),

    operations: () => client.get<OperationsSummary>("/api/v1/admin/operations"),

    purgeCache: (dataset) => client.post<PurgeResponse>(`/api/v1/admin/cache/${dataset}/purge`),
  };
}
