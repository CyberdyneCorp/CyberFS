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

test("an operator can sign in with a password", async ({ page }) => {
  await stubBackends(page);
  await page.goto("/audit");
  await expect(page).toHaveURL(/\/login$/);

  await page.getByLabel("Email").fill("admin@cyberdyne.io");
  await page.getByLabel("Password").fill("correct-horse");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  // Lands on the deep link that sent them to sign in, not a generic home page.
  await expect(page).toHaveURL(/\/audit$/);
});

test("a rejected password keeps the operator on the sign-in page", async ({ page }) => {
  await stubBackends(page, {
    login: { status: 401, body: { detail: "Invalid credentials" } },
  });
  await page.goto("/login");

  await page.getByLabel("Email").fill("admin@cyberdyne.io");
  await page.getByLabel("Password").fill("wrong");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  await expect(page.getByRole("alert")).toContainText("not correct");
  await expect(page).toHaveURL(/\/login$/);
});

test("a second factor is requested and completed", async ({ page }) => {
  await stubBackends(page, {
    login: { status: 200, body: { mfa_required: true, mfa_token: "mfa-1" } },
  });
  await page.goto("/login");

  await page.getByLabel("Email").fill("admin@cyberdyne.io");
  await page.getByLabel("Password").fill("correct-horse");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  const code = page.getByLabel("Authentication code");
  await expect(code).toBeVisible();
  await code.fill("123456");
  await page.getByRole("button", { name: "Verify" }).click();

  await expect(page).toHaveURL(/\/$/);
});

test("an expired second-factor challenge sends the operator back to the start", async ({
  page,
}) => {
  await stubBackends(page, {
    login: { status: 200, body: { mfa_required: true, mfa_token: "mfa-1" } },
    mfaVerify: { status: 401, body: { detail: "MFA session expired" } },
  });
  await page.goto("/login");

  await page.getByLabel("Email").fill("admin@cyberdyne.io");
  await page.getByLabel("Password").fill("correct-horse");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Authentication code").fill("123456");
  await page.getByRole("button", { name: "Verify" }).click();

  // A code can never succeed against a dead challenge, so the form must reset
  // rather than leaving the operator retyping codes forever.
  await expect(page.getByRole("alert")).toContainText("expired");
  await expect(page.getByLabel("Password")).toBeVisible();
});

test("a non-administrator who signs in with a password is refused", async ({ page }) => {
  await stubBackends(page, { isAdmin: false });
  await page.goto("/login");

  await page.getByLabel("Email").fill("bob@cyberdyne.io");
  await page.getByLabel("Password").fill("correct-horse");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByRole("heading", { name: "Access denied" })).toBeVisible();
});

test("neither the password nor the code reaches storage", async ({ page }) => {
  await stubBackends(page, {
    login: { status: 200, body: { mfa_required: true, mfa_token: "mfa-1" } },
  });
  await page.goto("/login");

  await page.getByLabel("Email").fill("admin@cyberdyne.io");
  await page.getByLabel("Password").fill("correct-horse");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByLabel("Authentication code").fill("123456");
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page).toHaveURL(/\/$/);

  const dumped = await page.evaluate(() => {
    const read = (s: Storage) => JSON.stringify(Object.entries(s));
    return `${read(sessionStorage)}${read(localStorage)}${location.href}`;
  });
  expect(dumped).not.toContain("correct-horse");
  expect(dumped).not.toContain("123456");
  expect(dumped).not.toContain("mfa-1");
});

test("signing out clears the session", async ({ dashboard }) => {
  await dashboard.goto("/");
  await dashboard.getByRole("button", { name: "Sign out" }).click();

  await expect(dashboard).toHaveURL(/\/login$/);
  const token = await dashboard.evaluate(() => sessionStorage.getItem("cyberfs.access_token"));
  expect(token).toBeNull();
});
