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

The dashboard is on a **different origin** from CyberdyneAuth, so cookies are
not available to it. It uses CyberdyneAuth's documented **fragment mode**
(`docs/oauth-external-clients.md` in that repo): tokens come back in the URL
fragment, are moved into `sessionStorage`, and the fragment is scrubbed from the
address bar immediately.

`is_admin` comes from `GET /api/v1/users/me` on CyberdyneAuth — the same flag
the CyberFS API enforces on every admin request, so the UI and the server cannot
disagree. A non-admin who signs in successfully lands on `/forbidden` rather
than being bounced back to a login they have already completed.

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
