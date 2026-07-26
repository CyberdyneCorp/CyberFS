// A signed-in dashboard with every backend response stubbed.
//
// The suite must run in CI without Postgres, MinIO, Redis, or CyberdyneAuth, so
// both origins the app talks to are intercepted here. Requests are fulfilled
// with CORS headers because the dashboard and the two APIs are separate origins
// in every environment, including this one.

import { test as base, type Page, type Route } from "@playwright/test";

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "*",
  "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
};

function json(route: Route, body: unknown, status = 200): Promise<void> {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: CORS,
    body: JSON.stringify(body),
  });
}

export function aUser(overrides: Record<string, unknown> = {}) {
  return {
    user_id: "u-1",
    subject: "alice@cyberdyne.io",
    quota_bytes: 10 * 1024 ** 3,
    used_bytes: 3 * 1024 ** 3,
    live_bytes: 2 * 1024 ** 3,
    trashed_bytes: 512 * 1024 ** 2,
    version_bytes: 512 * 1024 ** 2,
    percent_used: 30,
    over_quota: false,
    file_count: 128,
    folder_count: 14,
    encrypted_file_count: 96,
    encrypted_bytes: 2 * 1024 ** 3,
    encrypted_share: 75,
    grants_given: 3,
    grants_received: 1,
    is_admin: false,
    created_at: "2026-01-04T09:00:00Z",
    last_seen_at: "2026-07-23T11:30:00Z",
    ...overrides,
  };
}

const OVERVIEW = {
  total_bytes: 42 * 1024 ** 3,
  live_bytes: 36 * 1024 ** 3,
  trashed_bytes: 4 * 1024 ** 3,
  version_bytes: 2 * 1024 ** 3,
  file_count: 5120,
  folder_count: 310,
  user_count: 24,
  active_user_count: 17,
  encrypted_file_count: 3900,
  encrypted_bytes: 30 * 1024 ** 3,
  encrypted_share: 76.2,
  public_link_count: 6,
  grant_count: 48,
  content_types: [
    { content_type: "application/pdf", file_count: 1800, bytes: 12 * 1024 ** 3 },
    { content_type: "image/png", file_count: 2200, bytes: 9 * 1024 ** 3 },
  ],
  growth: [
    { day: "2026-07-21", files_added: 40, bytes_added: 900 * 1024 ** 2 },
    { day: "2026-07-22", files_added: 12, bytes_added: 210 * 1024 ** 2 },
    { day: "2026-07-23", files_added: 88, bytes_added: 1400 * 1024 ** 2 },
  ],
  top_consumers: [aUser(), aUser({ user_id: "u-2", subject: "bob@cyberdyne.io" })],
};

const LINKS = {
  items: [
    {
      id: "l-1",
      node_id: "n-1",
      created_by: "alice@cyberdyne.io",
      created_at: "2026-07-01T10:00:00Z",
      expires_at: "2026-07-26T10:00:00Z",
      revoked: false,
      passphrase_protected: false,
      access_count: 12,
      last_accessed_at: "2026-07-23T08:00:00Z",
    },
    {
      id: "l-2",
      node_id: "n-2",
      created_by: "bob@cyberdyne.io",
      created_at: "2026-06-20T10:00:00Z",
      expires_at: null,
      revoked: false,
      passphrase_protected: true,
      access_count: 3,
      last_accessed_at: null,
    },
  ],
};

const AUDIT = {
  items: [
    {
      action: "grant.created",
      occurred_at: "2026-07-23T12:00:00Z",
      actor_subject: "alice@cyberdyne.io",
      target_id: "n-1",
      recipient_subject: "bob@cyberdyne.io",
      reason: null,
      source_ip: "10.0.0.4",
      context: {},
    },
    {
      action: "link.revoked",
      occurred_at: "2026-07-22T09:14:00Z",
      actor_subject: "admin@cyberdyne.io",
      target_id: "l-9",
      recipient_subject: null,
      reason: "expired campaign",
      source_ip: "10.0.0.9",
      context: {},
    },
  ],
  next_cursor: null,
};

const OPERATIONS = {
  components: [
    { name: "postgres", status: "up", criticality: "required", latency_ms: 3, detail: null },
    { name: "minio", status: "up", criticality: "required", latency_ms: 11, detail: null },
    {
      name: "cache",
      status: "down",
      criticality: "optional",
      latency_ms: null,
      detail: "connection refused",
    },
  ],
  jobs: [
    {
      name: "purge",
      last_run_at: "2026-07-24T02:00:00Z",
      outcome: "success",
      duration_seconds: 4.2,
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
  cache: { available: false, keys: 0 },
  totals_reconcile: true,
};

export interface StubOptions {
  isAdmin?: boolean;
  /** How CyberdyneAuth answers `POST /auth/login`. Defaults to a token pair. */
  login?: { status: number; body: unknown };
  /** How it answers `POST /auth/mfa/verify`. Defaults to a token pair. */
  mfaVerify?: { status: number; body: unknown };
}

const TOKEN_PAIR = {
  access_token: "test-access-token",
  refresh_token: "test-refresh-token",
  token_type: "bearer",
};

/** Intercepts CyberdyneAuth and the CyberFS admin API for one page. */
export async function stubBackends(page: Page, options: StubOptions = {}): Promise<void> {
  const isAdmin = options.isAdmin ?? true;

  await page.route("**/api/v1/**", async (route) => {
    if (route.request().method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS, body: "" });
      return;
    }

    const path = new URL(route.request().url()).pathname;

    if (path === "/api/v1/users/me") {
      await json(route, { id: "admin-1", email: "admin@cyberdyne.io", is_admin: isAdmin });
    } else if (path === "/api/v1/auth/login") {
      const answer = options.login ?? { status: 200, body: TOKEN_PAIR };
      await json(route, answer.body, answer.status);
    } else if (path === "/api/v1/auth/mfa/verify") {
      const answer = options.mfaVerify ?? { status: 200, body: TOKEN_PAIR };
      await json(route, answer.body, answer.status);
    } else if (path === "/api/v1/admin/overview") {
      await json(route, OVERVIEW);
    } else if (path === "/api/v1/admin/users") {
      await json(route, {
        items: [
          aUser(),
          aUser({ user_id: "u-2", subject: "bob@cyberdyne.io", over_quota: true }),
        ],
      });
    } else if (path.startsWith("/api/v1/admin/users/")) {
      await json(route, aUser());
    } else if (path === "/api/v1/admin/links") {
      await json(route, LINKS);
    } else if (path === "/api/v1/admin/audit") {
      await json(route, AUDIT);
    } else if (path === "/api/v1/admin/operations") {
      await json(route, OPERATIONS);
    } else {
      await json(route, { detail: `unstubbed ${path}` }, 404);
    }
  });
}

/** Puts a token in `sessionStorage` before any app code runs. */
export async function signIn(page: Page): Promise<void> {
  await page.addInitScript(() => {
    sessionStorage.setItem("cyberfs.access_token", "test-access-token");
    sessionStorage.setItem("cyberfs.refresh_token", "test-refresh-token");
  });
}

export const test = base.extend<{ dashboard: Page }>({
  dashboard: async ({ page }, use) => {
    await signIn(page);
    await stubBackends(page);
    await use(page);
  },
});

export { expect } from "@playwright/test";
