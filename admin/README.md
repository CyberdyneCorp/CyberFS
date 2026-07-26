# CyberFS admin dashboard

A SvelteKit 2 / Svelte 5 single-page app for CyberFS administrators. It shows
storage totals, per-user usage, public links, the audit log, and dependency
health.

It never shows file contents, names, or previews. There is no admin path to
plaintext in CyberFS: file content is encrypted under per-user keys, and the
admin API exposes only counts and byte totals. If a field ever appears here that
could carry content, that is a bug in the API, not a feature of this app.

## Architecture

MVVM, enforced by lint rather than convention:

| Layer      | Where                          | Rule                                                                                                       |
| ---------- | ------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| View       | `src/routes/**/+page.svelte`   | Presentation only. No `fetch`, no filtering, no sorting.                                                   |
| View model | `src/routes/**/*.vm.svelte.ts` | All state, loading, error handling, filtering, sorting, pagination. Depends on the `AdminApi` _interface_. |
| API        | `src/lib/api/`                 | The only place that touches the network.                                                                   |

`eslint.config.js` bans `fetch` and imports of `$lib/api/client` inside
`*.svelte`. Because view models depend on an interface, every one of them is
unit-tested headlessly against `tests/stub-api.ts` — no DOM, no server.

## Authentication

There are two ways in, and both end holding the same CyberdyneAuth token pair in
`sessionStorage`. Everything after that point is identical.

**OAuth (preferred).** The dashboard is on a **different origin** from
CyberdyneAuth, so cookies are not available to it. It uses CyberdyneAuth's
documented **fragment mode** (`docs/oauth-external-clients.md` in that repo):
tokens come back in the URL fragment, are moved into `sessionStorage`, and the
fragment is scrubbed from the address bar immediately.

**Email and password.** `POST /api/v1/auth/login` on CyberdyneAuth, for
operators without a usable Cyberdyne identity. That endpoint answers with
_either_ a token pair _or_ `{mfa_required: true, mfa_token}`; on the latter the
page prompts for a one-time code and finishes through
`POST /api/v1/auth/mfa/verify`. The password and the code are held in component
state for the length of the request and never written to storage, the URL, or a
log.

Prefer OAuth where you can. The redirect flow never exposes a password to this
page, so cross-site scripting here could at worst steal a session token; with the
password form, it could capture an administrator credential that is reusable
across every Cyberdyne service. The hash-mode CSP (`script-src 'self'`, no inline
scripts) in `svelte.config.js` is the primary defence and should not be relaxed.
That is why the OAuth button is first and visually primary on the sign-in page.

Sign-in failures deliberately do not reveal whether an account exists: an unknown
address and a wrong password produce the same message, matching CyberdyneAuth,
which answers `401` for both and runs a dummy password verify so the two cost the
same. Rate limiting is CyberdyneAuth's (`RATELIMIT_LOGIN_PER_MIN`, per source IP,
counted before authenticating) — the dashboard surfaces a `429` distinctly rather
than reporting it as a bad password, and does not attempt its own throttle, which
an attacker would bypass by calling the API directly.

`is_admin` comes from `GET /api/v1/users/me` on CyberdyneAuth — the same flag
the CyberFS API enforces on every admin request, so the UI and the server cannot
disagree. A non-admin who signs in successfully lands on `/forbidden` rather
than being bounced back to a login they have already completed. This holds for
both sign-in paths.

### CyberdyneAuth must allowlist this app

Two settings on the CyberdyneAuth deployment, or login fails before the user
consents to anything:

```
OAUTH_CLIENT_REDIRECT_ALLOWLIST=https://fs-admin.example.com/auth/callback
CORS_ALLOWED_ORIGINS=["https://fs-admin.example.com"]
```

CyberdyneAuth also has to be running with `JWT_ALGORITHM=RS256` and
`OIDC_ENABLED=true`; under its defaults there is no JWKS and CyberFS cannot
verify anything. See `docs/local-auth-setup.md`.

## Configuration

Read at runtime from `$env/dynamic/public`, so one built image can be pointed at
any environment — which is how Coolify deploys it.

| Variable                    | Default                 | Meaning                                  |
| --------------------------- | ----------------------- | ---------------------------------------- |
| `PUBLIC_CYBERFS_API_URL`    | `http://localhost:8000` | CyberFS API origin                       |
| `PUBLIC_CYBERDYNE_AUTH_URL` | `http://localhost:8001` | CyberdyneAuth origin                     |
| `PUBLIC_OAUTH_PROVIDER`     | `google`                | Which provider the sign-in button starts |

Both origins must also appear in `connect-src` in `svelte.config.js`, or the
browser's CSP blocks the requests.

## Commands

```bash
npm install
npm run dev        # http://localhost:3002
npm run check      # svelte-check, strict
npm run lint       # prettier + eslint (includes the no-network-in-components rule)
npm test           # vitest: view models, API client, formatting, fragment parsing
npm run test:e2e   # playwright: auth flow + accessibility
npm run build      # adapter-node output in build/
```

`npm run test:e2e` stubs both backends, so it needs no Postgres, MinIO, Redis,
or CyberdyneAuth. It fails the build on any **serious** or **critical** axe
violation across every route, including the revoke confirmation, which only
exists after an interaction.

## Design system

The shared `@cyberdynecorp/svelte-ui-core` package is published to a private
GitHub registry that is not reachable from this checkout, so `src/app.css`
carries a small token-driven stylesheet instead. Components use semantic markup
and those tokens only; adopting the shared package later should be a styling
change, not a restructuring.
