## Why

The dashboard offers exactly one way in: "Continue with Cyberdyne", which delegates
to CyberdyneAuth's Google flow. Any operator without a usable Google identity --
a break-glass account, a service operator, an environment where the Google client
is misconfigured -- cannot reach the admin surface at all. CyberdyneAuth already
authenticates email/password directly (`POST /api/v1/auth/login`), so the
capability exists; the dashboard simply does not expose it.

## What Changes

- The login page gains an email/password form alongside the existing OAuth button.
  Both paths end in the same place: a CyberdyneAuth access/refresh token pair
  adopted into the existing session, with `is_admin` still decided by
  CyberdyneAuth rather than by the dashboard.
- Password sign-in handles the second factor. `POST /api/v1/auth/login` returns
  *either* a token pair *or* `{mfa_required: true, mfa_token}`; on the latter the
  dashboard prompts for a TOTP code and completes via `POST /api/v1/auth/mfa/verify`.
  Without this, any operator with MFA enabled would be unable to finish signing in.
- Failed sign-in reports a single indistinct message for both "no such account"
  and "wrong password", so the form cannot be used to enumerate accounts.
- The password and TOTP code are held only for the duration of the request and are
  never written to session storage, logs, or the URL.

Not changing: the OAuth button, the API, and the token format. This adds a second
way to obtain a token; everything downstream of holding one is untouched.

## Capabilities

### New Capabilities

None. This extends how an existing surface is entered rather than introducing a
new capability.

### Modified Capabilities

- `admin-dashboard`: "Dashboard access and session behaviour" currently requires
  that an unauthenticated visitor be redirected into the CyberdyneAuth login flow.
  It gains a password sign-in path, the MFA challenge step, and the
  non-enumerable-failure rule.

## Impact

**Affected code** -- dashboard only:

- `admin/src/routes/login/+page.svelte` -- the form, its validation, and the MFA step.
- `admin/src/lib/auth/auth-client.ts` -- `login()` and `verifyMfa()` calls.
- `admin/src/lib/app.ts` -- a `beginPasswordLogin()` composition entry alongside
  `beginLogin()`.
- `admin/tests/` -- coverage for both outcomes of `/auth/login`, the MFA path, and
  the indistinct-failure rule.

**Not affected**: the CyberFS API (`src/cyberfs/`) needs no change -- it validates
bearer tokens and never learns how they were obtained. No new environment
variable, no new CyberdyneAuth OAuth client, no CSP change (`connect-src` already
names the CyberdyneAuth origin).

**Security posture** -- this is the real cost and is accepted deliberately. Today
the dashboard never sees a password; credentials only ever reach CyberdyneAuth
through a redirect. A password form makes the admin console a place where
credentials are typed, so a cross-site scripting flaw there escalates from
"steal a session token" to "harvest an administrator password". The existing CSP
(`script-src 'self'`, hash-mode, no inline scripts) is the primary mitigation and
must not be relaxed. Operators who prefer the stronger posture can continue using
the OAuth button, which remains the default and visually primary action.

**Dependency**: CyberdyneAuth's rate limiting on `/api/v1/auth/login` is what
bounds password guessing. The dashboard cannot enforce this itself and must
surface a `429` clearly rather than masking it as a generic failure.
