# Integrating CyberFS with CyberdyneAuth

CyberFS is an OAuth2/OIDC **resource server**. It does not store passwords, run
its own login flow, or keep an admin role table — identity is entirely
CyberdyneAuth's.

## The one rule

Everything CyberFS validates is derived from
`${CYBERDYNE_AUTH_BASE_URL}/.well-known/openid-configuration`: the issuer, the
JWKS URI, and the accepted signing algorithms. None of them is hard-coded.

This is not stylistic. CyberdyneAuth's `docs/token-verification.md` documents
an incident (#47/#114) in which relying parties that hard-coded
`iss = "cyberdyne-auth"` broke when a correctness fix aligned the server with
its own discovery document. `domain/auth/policy.py` takes the discovered values
as arguments precisely so there is nowhere to bake a constant in, and
`tests/unit/test_auth_policy.py` asserts that the old hard-coded value is
rejected.

## CyberdyneAuth must run RS256 + OIDC (hard prerequisite)

**Both of these default to off.** With the defaults, CyberFS cannot authenticate
anyone at all:

| Setting on CyberdyneAuth | Default | Required for CyberFS |
|---|---|---|
| `JWT_ALGORITHM` | `HS256` | **`RS256`** |
| `OIDC_ENABLED` | `false` | **`true`** |
| `OIDC_ISSUER` | empty | the instance's public URL |
| `JWT_PRIVATE_KEY_PATH` / `JWT_PUBLIC_KEY_PATH` | empty | RSA keypair |

Why each matters:

- **`JWT_ALGORITHM=HS256` publishes no JWKS.** HS256 is symmetric — there is no
  public half to publish, and CyberdyneAuth sets no `kid` header. A resource
  server cannot verify a symmetric token without holding the signing secret,
  which would make it a token *issuer*, not a verifier. Verification would be
  impossible and CyberFS would be reduced to introspecting every single request.
- **`OIDC_ENABLED=false` 404s the discovery document** (and the JWKS endpoint).
  Since CyberFS derives issuer, JWKS URI, and algorithms from discovery, a 404
  means no verification is possible at all. Readiness reports the auth
  dependency as failed and every authenticated request returns `503`.
- **`OIDC_ISSUER` must be the public URL**, because the `iss` CyberdyneAuth
  *signs into tokens* is `oidc_issuer or jwt_issuer`. If it is unset, tokens are
  signed with the bare fallback name while discovery advertises something else,
  and issuer validation fails for every token — the #47/#114 failure mode
  reproduced from the other side.

Verify a target instance before pointing CyberFS at it:

```bash
curl -s "$BASE/.well-known/openid-configuration" | jq '.issuer, .jwks_uri, .id_token_signing_alg_values_supported'
curl -s "$BASE/.well-known/jwks.json" | jq '.keys | length'   # must be >= 1
```

## Provisioning the CyberFS client (required before deployment)

CyberFS needs an OAuth2 **client-credentials** client at CyberdyneAuth. It uses
it to obtain a service token, which it presents when calling the RFC 7662
introspection endpoint.

Run CyberdyneAuth's `provision-oauth-client` skill (or its
`.claude/skills/provision-oauth-client/provision_client.sh`) against the target
environment, requesting a client that may introspect tokens. It returns a client
id and secret:

| Setting | Value |
|---|---|
| `CYBERFS_CLIENT_ID` | the returned client id, conventionally `cyberfs` |
| `CYBERFS_CLIENT_SECRET` | the returned secret — **a Coolify secret, never committed** |
| `CYBERDYNE_AUTH_BASE_URL` | e.g. `https://auth.backend.coolify.cyberdynecorp.ai` |

Without this, token *verification* still works (it needs only the public JWKS),
but every introspection-backed operation — admin routes, grants, revocations,
ownership transfer — fails closed with `503`. That is deliberate: see below.

For local work you can skip provisioning entirely with `AUTH_DEV_MODE=true`.

## Two verification modes

| Mode | Used by | Cost | Freshness |
|---|---|---|---|
| Claim-based | reads, uploads, ordinary writes | local, no round trip | up to one access-token lifetime stale |
| Introspection-backed | admin routes, grants, revocations, ownership transfer | one call to CyberdyneAuth | authoritative now |

The split exists because the failure modes differ. A download authorized against
an `is_admin` that is 15 minutes stale is acceptable. Granting permission, or
performing an admin action, on the strength of a stale claim is not — an
administrator demoted a minute ago must be denied on their next request, without
waiting for their token to expire.

In introspection-backed mode CyberFS verifies the signature locally **and**
introspects; the introspection result wins. A structurally valid token whose
`active` is `false`, or whose `is_admin` has since been revoked, is rejected.

**Introspection failures fail closed.** If CyberdyneAuth cannot be reached
during a revocation-sensitive operation, CyberFS returns `503` rather than
falling back to the local claim. Falling back would defeat the entire reason for
asking.

## Resilience

Discovery and JWKS documents are cached:

| Setting | Meaning |
|---|---|
| `OIDC_DISCOVERY_TTL_SECONDS` | normal reuse window for the discovery document |
| `CACHE_TTL_JWKS_SECONDS` | normal reuse window for the key set |
| `JWKS_STALE_MAX_SECONDS` | how far past the TTL a cached copy may still be used **while CyberdyneAuth is unreachable** |
| `JWKS_REFRESH_COOLDOWN_SECONDS` | minimum interval between refetches triggered by an unknown `kid` |

A brief CyberdyneAuth outage with a warm cache is not an outage for CyberFS:
tokens keep verifying against the cached key set. A cold cache plus an
unreachable auth service is a `503` with `Retry-After`, and readiness reports
the dependency as failed.

An unknown `kid` is the normal signal that keys rotated, so it triggers one
refetch. The cooldown bounds that, so a flood of tokens naming a key that
genuinely does not exist cannot become a flood of requests. The cooldown tracks
*kid-triggered* refetches only — an ordinary TTL fetch does not arm it, or a
rotation occurring just after one would stay invisible until the cooldown lapsed.

## Claims CyberFS relies on

| Claim | Use |
|---|---|
| `type` | **which kind of token this is.** CyberdyneAuth signs `access`, `refresh`, `mfa`, and `service` tokens with the same key and the same issuer, so a signature check alone does not tell them apart. CyberFS accepts only `access` and `service`. Accepting an `mfa` token would be an authentication bypass: it is issued after the password step but *before* the second factor is verified. |
| `sub` | the identity. Ownership, grants, and quota all key off it, so it must stay stable across email and login-method changes. For a service token the subject is `client:<client_id>`; CyberFS normalises it back to the bare client id. |
| `is_admin` | gates `/api/v1/admin/**`. Never grants access to file content. |
| `org`, `orgs` | recorded on the local user record. A **missing** `orgs` claim means *no* org access, never all orgs — absence is a legacy token, not a wildcard. |
| `entitlements` | recorded; not yet used for quota sizing (an open question in `design.md`). |
| `client_id` | present with no `sub`, or with `sub == client_id`, marks a service principal. Service principals cannot own files or receive shares. |

`aud` is deliberately not required: CyberdyneAuth user tokens carry no audience
today, and demanding one would reject every real token.

## Local development

```bash
AUTH_DEV_MODE=true
```

Accepts any bearer token and reads the caller from it:

```
Authorization: Bearer dev:alice           # subject "alice"
Authorization: Bearer dev:alice:admin     # subject "alice", is_admin
Authorization: Bearer anything            # subject "dev-user"
```

Startup **fails** if this is set with `ENVIRONMENT` of `staging` or
`production` — the settings validator rejects it, so the stub is unreachable in
a deployment by construction, not by convention.
