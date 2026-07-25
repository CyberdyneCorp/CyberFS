import { defineConfig, devices } from "@playwright/test";

/**
 * The accessibility suite runs against a real build, but never against a real
 * backend: the tests stub every API response, so `npm run test:e2e` needs no
 * Postgres, no MinIO, and no CyberdyneAuth. That keeps it usable in CI, where
 * an a11y regression should fail the build rather than be skipped.
 */
export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "list" : "html",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run build && npm run preview -- --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
