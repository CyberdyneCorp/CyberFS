# CyberFS REST API

This is a hand-written orientation to the HTTP surface. The **authoritative,
machine-readable contract is the OpenAPI document** the application generates
from its own routers and Pydantic models — see [Generated OpenAPI
spec](#generated-openapi-spec). Where this page and the generated spec disagree,
the spec is correct.

Every route lives under one of two prefixes: unversioned operational endpoints
(`/health/*`, `/metrics`) and the versioned application surface under
`/api/v1`. A deployment mounted behind a path prefix sets `API_ROOT_PATH`, which
FastAPI applies to the served spec.

## Authentication

CyberFS is an OAuth2/OIDC **resource server** — it runs no login flow and stores
no passwords. Every authenticated request carries a CyberdyneAuth-issued bearer
token:

```
Authorization: Bearer <access-or-service-token>
```

A missing or empty header is rejected with a `401` problem detail in the same
shape as every other error (the bearer scheme is configured with
`auto_error=False` so the response stays uniform). See
[auth-integration.md](auth-integration.md) for how tokens are verified and
[local-auth-setup.md](local-auth-setup.md) for `AUTH_DEV_MODE`, which accepts
`Bearer dev:alice` / `Bearer dev:alice:admin` tokens for local work.

Three authentication modes gate the routes, implemented in
`src/cyberfs/adapters/inbound/api/dependencies.py`:

| Mode | How | Used by |
|---|---|---|
| Claim-based | verify the token signature locally | reads, uploads, ordinary writes |
| Introspection-backed | verify locally **and** introspect at CyberdyneAuth; the introspection result wins | grants, revocations, ownership transfer, public-link management |
| Admin | introspection-backed **and** `is_admin` | every `/api/v1/admin/**` route |

Introspection-backed operations **fail closed**: if CyberdyneAuth cannot be
reached, they return `503` rather than trusting a possibly stale claim.

The two public-link download routes (`GET /public/{token}` and
`GET /public/{token}/content`) are **deliberately unauthenticated** — the link
token is the credential. An optional `X-Link-Passphrase` header carries a
link's passphrase when one was set.

## Concurrency and conditional writes

Node reads publish an `ETag` header. Mutating routes (`rename`, `move`,
`delete`) accept an `If-Match` header carrying that ETag; a stale value is
rejected with `412 Precondition Failed`. Downloads honor the `Range` header and
respond `206 Partial Content` with `Content-Range`.

## Endpoints

Source of truth for every path below:
`src/cyberfs/adapters/inbound/api/routers/`.

### Health — no authentication

| Method | Path | Purpose |
|---|---|---|
| GET | `/health/live` | Process liveness; never consults dependencies. |
| GET | `/health/ready` | Dependency readiness; `503` when any critical component is down. |

### Metrics

| Method | Path | Purpose |
|---|---|---|
| GET | `/metrics` | Prometheus exposition. Only served to internal clients (others get `404`); omitted from the OpenAPI schema; disabled when `METRICS_ENABLED=false`. |

### Nodes / folders — `nodes.py` (claim-based)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/nodes/root` | The caller's root folder. |
| GET | `/api/v1/nodes/{node_id}` | Node metadata. |
| GET | `/api/v1/nodes/{node_id}/children` | List a folder's children (cursor-paginated: `limit`, `cursor`). |
| POST | `/api/v1/nodes/{node_id}/folders` | Create a folder. |
| PATCH | `/api/v1/nodes/{node_id}/name` | Rename a node. |
| PATCH | `/api/v1/nodes/{node_id}/parent` | Move a node to another parent. |
| POST | `/api/v1/nodes/{node_id}/copy` | Copy a node into another folder (content duplicated server-side). |
| DELETE | `/api/v1/nodes/{node_id}` | Move to trash. |
| POST | `/api/v1/nodes/{node_id}/restore` | Restore from trash — brings back the whole subtree that deletion removed. |
| GET | `/api/v1/search` | Search node metadata (`q`, `limit`). |

### Trash — `trash.py` (claim-based)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/trash` | The caller's own trash, newest deletion first (cursor-paginated: `limit`, `cursor`). |
| POST | `/api/v1/trash/purge` | Destroy every entry in the caller's trash. Irreversible, bounded, count-guarded. |

The trash is **one entry per deletion**, not one per affected node: deleting a
folder of four hundred files produces a single entry, for the folder. Each entry
carries the path the node occupied, when it was deleted, when the retention
sweep will destroy it (`TRASH_RETENTION_DAYS`), and the total content bytes and
node count that restoring it would bring back — so a client never has to read a
trashed node individually, which the API deliberately refuses (`404`).

`POST /api/v1/nodes/{node_id}/restore` on an entry brings back **every node the
same deletion removed**, each with its revision advanced, so an `If-Match` taken
before the deletion no longer matches. A node deleted separately *before* its
parent stays deleted, and reappears as its own trash entry once the parent is
live again.

The trash is the owner's alone. A share recipient sees nothing — a soft delete
withdraws access — and there is no path or query parameter naming another user.
No administrative counterpart exists: node names are gated on the admin surface
behind `ADMIN_SHOW_FILENAMES`, and a trash listing there would hand over names,
paths, and sizes with no gate at all. `POST /api/v1/nodes/{id}/purge` remains the
administrative path for a node named in an audit record.

#### Emptying the trash

```
POST /api/v1/trash/purge
{"expected_entries": 7}
```

`expected_entries` is required and is checked against the trash as it stands: a
mismatch is `409 Conflict` and destroys nothing. The count can only be right if
the caller listed the trash first, which is the point — a `confirm: true` flag
would be a constant no client could get wrong, and therefore no evidence that
anybody looked.

One call destroys at most `PAGE_SIZE_MAX` entries, oldest deletion first, and
reports what is left:

```json
{"entries_purged": 7, "nodes_destroyed": 412, "objects_deleted": 380,
 "bytes_reclaimed": 91234567, "entries_remaining": 0}
```

So a large trash is a loop: empty, read `entries_remaining`, restate it, repeat
until it is zero. A blind retry after a successful call fails loudly with `409`
rather than destroying whatever has since been trashed.

Every node destroyed produces the same retained `node.purged` security record an
individual purge would, plus one `trash.emptied` record naming the batch.

### Content — `content.py` (claim-based)

| Method | Path | Purpose |
|---|---|---|
| PUT | `/api/v1/nodes/{node_id}/content` | Replace a file's content, creating a new version. |
| PUT | `/api/v1/nodes/{node_id}/files/{name}` | Upload a file into a folder (`?encrypted=` overrides the default). |
| PUT | `/api/v1/nodes/{node_id}/encryption` | Turn content encryption on or off for a file. |
| GET | `/api/v1/nodes/{node_id}/content` | Download a file (`Range` supported; `?version=` for an older one). |
| GET | `/api/v1/nodes/{node_id}/versions` | List content versions. |
| POST | `/api/v1/nodes/{node_id}/versions/{version_id}/restore` | Restore an earlier version as a new one. |

Bytes always transit the API — no presigned URLs are issued — so every read and
write passes through authorization, quota, and encryption. The download's
`Content-Length` is the plaintext length the caller receives, which differs from
the stored object's size when a file is encrypted.

### Sharing and public links — `shares.py`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| PUT | `/api/v1/nodes/{node_id}/grants` | fresh | Grant a role on a node. |
| GET | `/api/v1/nodes/{node_id}/grants` | claim | List grants. |
| DELETE | `/api/v1/nodes/{node_id}/grants/{subject}` | fresh | Revoke a grant. |
| GET | `/api/v1/shared-with-me` | claim | Nodes shared with the caller. |
| POST | `/api/v1/nodes/{node_id}/links` | fresh | Create a public link (returns the token once, only here). |
| GET | `/api/v1/nodes/{node_id}/links` | claim | List a node's public links. |
| DELETE | `/api/v1/links/{link_id}` | fresh | Revoke a public link. |
| GET | `/api/v1/public/{token}` | none | Open a public link (metadata). |
| GET | `/api/v1/public/{token}/content` | none | Download through a public link. |
| POST | `/api/v1/nodes/{node_id}/owner` | fresh | Transfer ownership. |

### Admin — `admin.py` (admin auth, prefix `/api/v1/admin`)

There is deliberately **no** admin route that returns file content, a preview,
or key material — `tests/unit/test_admin_router.py` enumerates this router and
fails if one ever appears.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/admin/overview` | Deployment-wide statistics (`growth_days`, `top_n`). |
| GET | `/api/v1/admin/users` | Per-user storage (`limit`, `sort_by`, `over_quota`). |
| GET | `/api/v1/admin/users/{user_id}` | One user's storage. |
| PUT | `/api/v1/admin/users/{user_id}/quota` | Set a user's quota. |
| GET | `/api/v1/admin/links` | Active public links across the deployment. |
| DELETE | `/api/v1/admin/links/{link_id}` | Revoke any public link. |
| DELETE | `/api/v1/admin/nodes/{node_id}/grants/{subject}` | Always `403`: grants belong to the node owner, not an admin. |
| GET | `/api/v1/admin/audit` | Browse the audit log (`actor`, `action`, `target`, `since`, `until`, `limit`, `cursor`). |
| GET | `/api/v1/admin/operations` | Component health, background jobs, cache stats, backup summary. |
| POST | `/api/v1/admin/operations/backup` | Trigger a backup by hand (`409` if one is already running; refused when backups are disabled). |
| GET | `/api/v1/admin/operations/backups` | List backups. |
| POST | `/api/v1/admin/cache/{dataset}/purge` | Purge a cache dataset (reports how many keys went, never what they held). |

## Errors: RFC 7807 problem details

Every error — including `401` for a missing token — is returned as a JSON
problem document with media type `application/problem+json`, built in
`src/cyberfs/adapters/inbound/api/errors.py`:

```json
{
  "type": "https://cyberfs.cyberdynecorp.ai/errors/NODE_NOT_FOUND",
  "title": "Not found",
  "status": 404,
  "code": "NODE_NOT_FOUND",
  "detail": "no such node",
  "request_id": "01J..."
}
```

| Field | Meaning |
|---|---|
| `type` | A URI derived from the stable domain `code`. |
| `title` | Short, human-readable summary of the error class. |
| `status` | The HTTP status code, repeated in the body. |
| `code` | The stable domain error code — **branch on this**, not on prose. |
| `detail` | A caller-safe message. Unexpected exceptions never leak their text. |
| `request_id` | Present when a request id is bound, so a report traces to a log line. |

Retryable statuses (`429 Too Many Requests`, `503 Service Unavailable`) also
carry a `Retry-After` header. Domain errors map to statuses as follows:

| Status | Domain errors |
|---|---|
| 401 | token expired / invalid / authentication |
| 403 | permission denied |
| 404 | not found, unknown recipient |
| 409 | conflict |
| 412 | precondition failed (stale `If-Match`) |
| 413 | payload too large |
| 422 | validation |
| 429 | rate limited |
| 507 | quota exceeded |
| 503 | dependency unavailable |
| 500 | key unavailable, integrity failure, unexpected |

## Generated OpenAPI spec

The generated document is authoritative. Regenerate it with the justfile recipe,
which imports the app factory and dumps its OpenAPI:

```bash
just openapi > openapi.json
```

Equivalently:

```bash
uv run python -c "import json; from cyberfs.adapters.inbound.api.app import create_app; print(json.dumps(create_app().openapi(), indent=2))"
```

A running instance also serves interactive docs at `/docs` (Swagger UI) and the
raw spec at `/openapi.json`, both offset by `API_ROOT_PATH` when set.
