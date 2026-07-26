// Accessibility gate.
//
// `admin-dashboard/spec.md` requires automated checks across every route, and
// requires the build to fail on serious or critical violations. Anything
// milder is reported in the failure message when a test does fail, but does not
// on its own break CI.

import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

import { expect, signIn, stubBackends, test } from "./fixtures";

const BLOCKING = new Set(["serious", "critical"]);

async function expectNoBlockingViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();

  const blocking = results.violations.filter((v) => BLOCKING.has(v.impact ?? ""));
  const described = blocking.map(
    (v) =>
      `${v.impact}: ${v.id} — ${v.help} (${v.nodes.length} nodes)\n  ${v.nodes[0]?.html ?? ""}`,
  );

  expect(described, described.join("\n")).toEqual([]);
}

const AUTHENTICATED_ROUTES = [
  { path: "/", heading: "Overview" },
  { path: "/users", heading: "Users" },
  { path: "/users/u-1", heading: "alice@cyberdyne.io" },
  { path: "/sharing", heading: "Sharing" },
  { path: "/audit", heading: "Audit" },
  { path: "/health", heading: "Health" },
];

for (const route of AUTHENTICATED_ROUTES) {
  test(`${route.path} has no serious or critical violations`, async ({ dashboard }) => {
    await dashboard.goto(route.path);
    // Wait for the data, not just the shell: an empty table can pass checks a
    // populated one fails.
    await expect(
      dashboard.getByRole("heading", { name: route.heading, level: 1 }),
    ).toBeVisible();
    await expect(dashboard.getByText("Loading…")).toHaveCount(0);

    await expectNoBlockingViolations(dashboard);
  });
}

test("the login page has no serious or critical violations", async ({ page }) => {
  await stubBackends(page);
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "CyberFS Administration" })).toBeVisible();

  await expectNoBlockingViolations(page);
});

test("the second-factor prompt has no serious or critical violations", async ({ page }) => {
  // Like the revoke confirmation, this appears only on interaction, so a
  // page-load-only sweep would never reach it.
  await stubBackends(page, {
    login: { status: 200, body: { mfa_required: true, mfa_token: "mfa-1" } },
  });
  await page.goto("/login");
  await page.getByLabel("Email").fill("admin@cyberdyne.io");
  await page.getByLabel("Password").fill("correct-horse");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByLabel("Authentication code")).toBeVisible();

  await expectNoBlockingViolations(page);
});

test("the access-denied page has no serious or critical violations", async ({ page }) => {
  await signIn(page);
  await stubBackends(page, { isAdmin: false });

  await page.goto("/");
  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByRole("heading", { name: "Access denied" })).toBeVisible();

  await expectNoBlockingViolations(page);
});

test("the revoke confirmation has no serious or critical violations", async ({ dashboard }) => {
  // Destructive confirmations appear on interaction, so a page-load-only sweep
  // would never see this one.
  await dashboard.goto("/sharing");
  await dashboard.getByRole("button", { name: /Revoke link created by alice/ }).click();
  await expect(dashboard.getByRole("heading", { name: "Revoke this link?" })).toBeVisible();

  await expectNoBlockingViolations(dashboard);
});
