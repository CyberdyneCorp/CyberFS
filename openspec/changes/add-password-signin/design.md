## Context

The dashboard holds a bearer token obtained through CyberdyneAuth's fragment-mode
OAuth flow and sends it on every CyberFS request (`admin/src/lib/api/client.ts`).
Sign-in today is one call: `AuthClient.authorizationUrl()` asks CyberdyneAuth for a
Google authorization URL, the browser leaves, and `/auth/callback` parses the
returned tokens out of the URL fragment (`admin/src/lib/auth/fragment.ts`) and
adopts them via `adoptTokens()`.

CyberdyneAuth already supports direct credentials. `POST /api/v1/auth/login` takes
`{email, password}` and returns **one of two shapes**:

- `TokenResponse` -- `{access_token, refresh_token, token_type}`
- `MfaRequiredResponse` -- `{mfa_required: true, mfa_token}`
  (`CyberdyneAuth/src/cyberdyne_auth/adapters/inbound/api/schemas.py:115`)

The second is completed by `POST /api/v1/auth/mfa/verify` with
`{mfa_token, code}`, which returns a `TokenResponse`.

Constraint: the dashboard's CSP is hash-mode with `script-src 'self'` and already
names the CyberdyneAuth origin in `connect-src` (`admin/svelte.config.js`), so no
CSP change is needed and none should be made -- it is the main defence for a page
that will now accept typed credentials.

## Goals / Non-Goals

**Goals:**

- A second way to obtain a CyberdyneAuth token, converging on the existing session
  handling so nothing downstream of `adoptTokens()` changes.
- Correct handling of the MFA branch, since an operator with a second factor
  otherwise cannot complete sign-in at all.
- Sign-in failures that neither enumerate accounts nor mislabel rate limiting.

**Non-Goals:**

- Registration, password reset, and password change. CyberdyneAuth exposes these
  (`/auth/register`, `/auth/password/reset-request`, `/auth/password/change`) but
  the admin dashboard is not the place to run account lifecycle.
- Enrolling or managing MFA. The dashboard consumes an existing challenge; it does
  not set one up.
- Wallet sign-in (`/auth/wallet/*`), which is a separate mechanism.
- Any change to the CyberFS API. It validates bearer tokens and is indifferent to
  how they were minted.
- "Remember me" or any extension of token lifetime.

## Decisions

**Reuse `AuthClient` rather than a new module.** `AuthClient` is already the single
boundary to CyberdyneAuth and already maps transport failures to `NetworkError`
with correct service attribution. Adding `login()` and `verifyMfa()` there keeps
one place that knows the auth origin. The alternative -- a separate
`PasswordClient` -- would duplicate the error mapping and give two answers to
"where does the dashboard call auth".

**Discriminate the login response on `mfa_required`, not on shape-sniffing.**
The server sets `mfa_required: true` explicitly on the challenge, so branch on that
field. Inferring from "did I get an `access_token`" would silently treat a
malformed response as a successful challenge.

**Model the form as an explicit state machine**: `credentials -> awaiting-code ->
done`, with the `mfa_token` held only in component state for the `awaiting-code`
step. It is a short-lived credential and belongs in neither `sessionStorage` (where
the access token lives) nor the URL. This also keeps the component from having to
guess whether a submit means "password" or "code".

**Keep OAuth the primary action.** The button stays first and visually primary;
the password form sits below it. The redirect flow never exposes a password to the
dashboard, so it remains the better path and should read as the default. This is a
presentation decision with a security rationale, which is why it is recorded here
rather than left to whoever builds the page.

**Map failures at the client, not in the component.** `AuthClient.send()` already
throws on non-2xx with the server's `detail`. Password sign-in needs three
distinguishable outcomes -- rejected credentials, rate limited, everything else --
so `login()` maps `401` to a single indistinct rejection and `429` to a rate-limit
error, letting `describeError` render both. Leaving this to the component would
put security-relevant wording in a `.svelte` file where it is easy to vary by
accident.

**Do not pre-validate the password client-side** beyond "not empty". Rules like a
minimum length leak the server's policy and drift from it; CyberdyneAuth is the
authority on whether a credential is acceptable.

## Risks / Trade-offs

- **A password form widens the blast radius of XSS on the admin console.** Today a
  script injection could steal a session token; afterwards it could capture an
  administrator password, which is reusable across every Cyberdyne service.
  → Mitigation: the existing hash-mode CSP with `script-src 'self'` and no inline
  scripts stays as-is and must not be relaxed; no third-party script is introduced
  by this change; OAuth remains the primary, recommended path.

- **Password guessing against a known admin email.** The dashboard cannot bound
  this; only CyberdyneAuth can.
  → Mitigation: rely on and surface CyberdyneAuth's rate limiting rather than
  inventing a client-side counter that an attacker simply bypasses by calling the
  API directly. Treat a missing rate limit on `/auth/login` as a CyberdyneAuth bug
  to be fixed there.

- **Indistinct failure messages are worse for legitimate operators**, who cannot
  tell a typo'd address from a typo'd password.
  → Mitigation: accepted deliberately. Account enumeration on an admin console is
  the larger harm.

- **The MFA branch is easy to leave untested** because the account used to build the
  feature probably has no second factor.
  → Mitigation: the specs make it a required scenario, and the tasks cover it with
  a stubbed challenge response rather than a live MFA account.

## Migration Plan

Additive and independently deployable. No environment variable, no CyberdyneAuth
OAuth client, no API change, no data migration. Deploying is a dashboard rebuild;
rolling back is redeploying the previous image, which leaves the OAuth flow --
untouched by this change -- working throughout.

## Open Questions

- Should password sign-in be gated behind a flag so it can be disabled per
  environment? Not proposed, since the dashboard reads only `PUBLIC_*` config and
  adding one would need a new variable, but it is the natural lever if the
  security tradeoff is later judged unacceptable for production.
- CyberdyneAuth's rate-limit response for `/auth/login` has not been observed
  directly; the implementation should confirm it is a `429` with `Retry-After`
  and adjust the mapping if it differs.
