## 1. Auth client

- [x] 1.1 Add a `LoginOutcome` type to `admin/src/lib/auth/auth-client.ts` that discriminates a token pair from an MFA challenge on the `mfa_required` field
- [x] 1.2 Add `AuthClient.login(email, password)` calling `POST /api/v1/auth/login`, returning that discriminated outcome
- [x] 1.3 Add `AuthClient.verifyMfa(mfaToken, code)` calling `POST /api/v1/auth/mfa/verify`, returning a token pair
- [x] 1.4 Map `401` from either call to a single indistinct rejection error, and `429` to a distinct rate-limit error carrying `Retry-After` when present
- [x] 1.5 Confirm against the running CyberdyneAuth that a rate-limited login is in fact `429` with `Retry-After`, and correct the mapping if it is not (design.md, Open Questions)

## 2. Composition

- [x] 2.1 Add `beginPasswordLogin(email, password)` to `admin/src/lib/app.ts` returning either "signed in" or "code required" plus the challenge token
- [x] 2.2 Add `completeMfaLogin(mfaToken, code)` to `admin/src/lib/app.ts`
- [x] 2.3 Have both adopt tokens through the existing `adoptTokens()` and resolve admin status through the existing `loadProfile()`, so the OAuth and password paths converge
- [x] 2.4 Preserve the return path across password sign-in the same way `beginLogin()` does, so a deep link survives

## 3. Login page

- [x] 3.1 Add the email/password form to `admin/src/routes/login/+page.svelte`, below the OAuth button, which stays first and visually primary
- [x] 3.2 Implement the `credentials -> awaiting-code -> done` state machine, holding `mfa_token` in component state only
- [x] 3.3 Render the one-time-code prompt for the `awaiting-code` state, keeping the operator there on a rejected code
- [x] 3.4 Require a non-empty email and password before submitting, and add no other client-side password rules
- [x] 3.5 Disable the submit control while a request is in flight, so a double submit cannot fire two attempts
- [x] 3.6 Route all failures through `describeError` so wording stays in one place

## 4. Tests

- [x] 4.1 `AuthClient.login()` returns a token pair when CyberdyneAuth answers with tokens
- [x] 4.2 `AuthClient.login()` returns a challenge when CyberdyneAuth answers `mfa_required`
- [x] 4.3 `AuthClient.verifyMfa()` completes a challenge into a token pair
- [x] 4.4 A wrong password and an unknown account produce the identical message (the non-enumeration rule)
- [x] 4.5 A rate-limited attempt reports rate limiting, not a wrong password
- [x] 4.6 A network failure during password sign-in is attributed to CyberdyneAuth, not CyberFS
- [x] 4.7 A valid password for a non-admin profile is refused like any other non-admin
- [x] 4.8 After a failed and a successful attempt, neither the password nor the code appears in session storage
- [x] 4.9 The login route still passes the accessibility checks in `admin/e2e/a11y.spec.ts` with the form present, including labels on both fields

## 5. Verification and documentation

- [x] 5.1 `npm run check`, `npm run lint`, and `npm run test` all clean in `admin/`
- [x] 5.2 Sign in against the deployed CyberdyneAuth with a real password account and confirm the dashboard admits an administrator
- [x] 5.3 Confirm the OAuth button still works unchanged after the page is restructured
- [x] 5.4 Update `admin/README.md` to describe both sign-in paths and state why OAuth is preferred
- [ ] 5.5 Run `openspec validate add-password-signin` and archive the change once deployed
