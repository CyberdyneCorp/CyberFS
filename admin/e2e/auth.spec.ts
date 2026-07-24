// The authentication guard, end to end.

import { expect, signIn, stubBackends, test } from "./fixtures";

test("an unauthenticated visitor is sent to sign in", async ({ page }) => {
  await stubBackends(page);
  await page.goto("/users");

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("button", { name: /Continue with Cyberdyne/ })).toBeVisible();
});

test("a deep link survives the trip through the identity provider", async ({ page }) => {
  await stubBackends(page);
  await page.goto("/audit");
  await expect(page).toHaveURL(/\/login$/);

  // Stand in for CyberdyneAuth's redirect back with tokens in the fragment.
  await page.goto(
    "/auth/callback#access_token=a&refresh_token=r&token_type=Bearer&expires_in=900",
  );

  await expect(page).toHaveURL(/\/audit$/);
  // The fragment must not survive: it would otherwise be bookmarkable.
  expect(new URL(page.url()).hash).toBe("");
});

test("an authenticated non-administrator is told why, not asked to sign in again", async ({
  page,
}) => {
  await signIn(page);
  await stubBackends(page, { isAdmin: false });

  await page.goto("/users");

  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByRole("heading", { name: "Access denied" })).toBeVisible();
});

test("a failed handshake reports the provider's reason", async ({ page }) => {
  await stubBackends(page);
  await page.goto("/auth/callback#error=access_denied&error_description=You+cancelled+sign-in");

  await expect(page.getByRole("alert")).toContainText("You cancelled sign-in");
});

test("signing out clears the session", async ({ dashboard }) => {
  await dashboard.goto("/");
  await dashboard.getByRole("button", { name: "Sign out" }).click();

  await expect(dashboard).toHaveURL(/\/login$/);
  const token = await dashboard.evaluate(() => sessionStorage.getItem("cyberfs.access_token"));
  expect(token).toBeNull();
});
