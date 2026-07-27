# CyberFS

Backend filesystem service with sharing, optional content encryption, and usage
analytics. CyberFS stores a per-user tree of folders and files, versions their
content in object storage, and exposes it over a versioned HTTP API. Identity is
delegated entirely to [CyberdyneAuth](docs/auth-integration.md) — CyberFS is an
OAuth2/OIDC **resource server**; it keeps no passwords and runs no login flow.

Highlights:

- **Hierarchical filesystem** — folders, files, rename/move, trash with
  retention, on-demand purge, content versioning, and metadata search
  (`/api/v1/nodes`, `/api/v1/search`).
- **Trash you can actually browse** — `GET /api/v1/trash` lists the caller's own
  deletions, one entry per deletion rather than one per affected node, each
  carrying its original path, its retention deadline, and the bytes and node
  count restoring it would bring back. Restoring an entry brings back the whole
  subtree that deletion removed. `POST /api/v1/trash/purge` empties the trash in
  bounded, count-confirmed steps. See [the API guide](docs/api.md#trash--trashpy-claim-based).
- **Tags and metadata** — a label set and key/value pairs on any node, searchable
  alongside the name (`PUT /api/v1/nodes/{id}/tags`,
  `PUT /api/v1/nodes/{id}/metadata`). Both replace wholesale rather than merging,
  so an empty list clears them. Search filters combine with AND: repeating `tag`
  requires every one of them, and `value` pins the `key` it accompanies.
  **Tags and metadata are stored unencrypted**, because searchable means indexed
  — anything placed in them is readable by whoever can read the database, and by
  administrators. File *content* stays encrypted and is never indexed.
- **Partial label updates** — `PATCH /api/v1/nodes/{id}/tags` takes `add` and
  `remove`; `PATCH /api/v1/nodes/{id}/metadata` takes `set` pairs and `remove`
  keys. They *merge*: nothing the request does not name is touched, so
  contributing one tag costs no read-modify-write round trip and two callers
  patching disjoint labels both land — the rows are written individually rather
  than as a whole-collection replace. A patch that turns out to change nothing is
  a success that writes nothing: no revision bump, no activity record, and the
  same `ETag` as before. Label writes to one node are applied one at a time,
  whether they arrive by `PATCH` or by `PUT` — that is what makes the per-node
  maximum a real bound and lets a patch know it changed nothing — so two writes
  that would jointly cross the maximum do not both succeed. Naming the same label
  as both an addition and a removal is refused rather than ordered, and a `PUT`
  still wins outright over every tag it does not name, because replacing states a
  complete collection while patching states a change to one.
- **Content digest** — every version carries the SHA-256 of its plaintext,
  reported on the node and on each version, so a client can verify what it
  downloaded. It is withheld from the admin surface: a plaintext hash would let a
  holder confirm which user has a specific known file even when it is encrypted.
- **Sharing** — per-node grants with roles, "shared with me", public links with
  rate-limited access, and ownership transfer (`/api/v1/shares`, see the
  [sharing spec](openspec/changes/bootstrap-cyberfs/specs/sharing/spec.md)).
- **Optional content encryption** — AES-256-GCM envelope encryption
  (`MASTER_KEY` → per-user KEK → per-file DEK), enabled per file or on by
  default, with online master-key rotation (see the
  [content-encryption spec](openspec/changes/bootstrap-cyberfs/specs/content-encryption/spec.md)).
- **Admin dashboard** — deployment-wide stats, per-user storage and quota, audit
  log, live public links, backups, and operations health
  (`/api/v1/admin`); a SvelteKit MVVM front end lives in [`admin/`](admin/).
- **WebDAV** — a Class 1 surface at `/webdav`, **on by default**, authenticated
  with an existing S3 access key over Basic. `rclone mount` over it is how CyberFS
  is exposed as a **FUSE** filesystem, so no driver ships here. Class 2 locking is
  refused rather than faked, which means Finder and Explorer mount read-only; see
  [`docs/webdav.md`](docs/webdav.md). Reverses a non-goal recorded in the bootstrap
  and S3 changes.
- **Backup & restore** — scheduled off-site backups to a separate S3 target and
  a verified restore pipeline (see [`docs/restore-runbook.md`](docs/restore-runbook.md)).

Specifications live in [`openspec/specs/`](openspec/specs/), one file per
capability, and are the living description of what CyberFS does. Proposed changes
sit in `openspec/changes/` until archived, at which point their deltas are merged
into those specs. `just ci` fails on a spec that does not validate.

Work that the specs require and that has **not been proved on real
infrastructure** is listed in
[`docs/outstanding-verification.md`](docs/outstanding-verification.md) — restoring
from a real backup, the multi-replica drill, and `MASTER_KEY` custody among them.

## Stack

- **Python 3.12** (`>=3.12,<3.13`), **FastAPI** + **uvicorn**
- **SQLAlchemy 2 (async)** on **asyncpg**, migrations with **Alembic**
- **Pydantic 2** / **pydantic-settings** for models and configuration
- **Redis** cache (degrades gracefully) and **MinIO** / S3 object storage
- **PyJWT[crypto]** + **cryptography** for token verification and AES-256-GCM
- **structlog** logging and **prometheus-client** metrics
- Tooling: **uv**, **ruff**, **mypy** (strict), **pytest**, **just**, **pre-commit**
- Admin dashboard: **SvelteKit** (see [`admin/README.md`](admin/README.md))

## Quick start

Prerequisites: [`uv`](https://docs.astral.sh/uv/), [`just`](https://github.com/casey/just),
and Docker (for Postgres, Redis, and MinIO via `docker-compose.yml`).

```sh
just install            # uv sync --all-extras + pre-commit hooks
cp .env.example .env    # dev defaults run out of the box
just dev                # db-up → db-wait → migrate → serve-dev
```

`just dev` brings up Postgres, Redis, and MinIO, waits for Postgres, applies
migrations, and starts the API on `http://0.0.0.0:8000` with reload. The
`.env.example` defaults ship a development `MASTER_KEY` placeholder (rejected in
production) and set `AUTH_DEV_MODE=false`; enable `AUTH_DEV_MODE=true` locally to
run without a live CyberdyneAuth, or wire up a real instance following
[`docs/local-auth-setup.md`](docs/local-auth-setup.md).

Once running:

- API docs: `http://localhost:8000/docs`
- Liveness / readiness: `GET /health/live`, `GET /health/ready`
- Metrics: `GET /metrics` (when `METRICS_ENABLED=true`)
- Generate the OpenAPI schema: `just openapi`

## Recipes

Common `just` recipes (run `just` for the full list):

| Recipe | What it does |
| --- | --- |
| `just install` | `uv sync --all-extras` and install pre-commit hooks |
| `just dev` | Zero-to-running: start deps, migrate, serve with reload |
| `just up` / `just down` / `just reset` | Start / stop the compose stack (`reset` wipes volumes) |
| `just logs [service]` / `just psql` | Tail compose logs / open a `psql` shell |
| `just migrate` / `just migrate-down` | Apply / roll back one Alembic migration |
| `just make-migration "msg"` | Autogenerate a migration |
| `just restore <backup_id> [destructive=true]` | Restore a named backup |
| `just test` / `just test-unit` / `just test-integration` | Run tests |
| `just test-cov` | Unit tests with coverage (term + HTML) |
| `just lint` / `just fmt` / `just typecheck` | ruff check / ruff format / mypy |
| `just check` | `lint typecheck test-cov` |
| `just ci` | `check` plus `openspec validate --all --strict` |
| `just openapi` | Print the OpenAPI schema |

## Architecture

CyberFS follows **hexagonal (ports and adapters)** architecture. Source is under
`src/cyberfs/`:

- **`domain/`** — pure business rules and value objects (keys, framing, sharing,
  permissions, links, auth policy) plus port protocols in `domain/ports/`. No
  I/O, no framework imports.
- **`application/`** — use-case services orchestrating the domain (nodes,
  content, sharing, encryption, admin, jobs, backup, restore, health).
- **`adapters/`** — inbound (FastAPI routers under
  `adapters/inbound/api/routers/`) and outbound (Postgres, Redis, MinIO, crypto,
  CyberdyneAuth HTTP) implementations of the ports.
- **`infrastructure/`** — settings, DB engine, logging, scheduler, and other
  wiring; the FastAPI application factory in `adapters/inbound/api/app.py` is the
  composition root.

The layering is enforced in code: `tests/unit/test_layering.py` fails if an
inner layer imports an outer one. See [`docs/architecture.md`](docs/architecture.md)
for the full ports-and-adapters map.

## Testing

- `just test-unit` — fast, I/O-free unit tests over `domain/` and `application/`.
- `just test-integration` — tests marked `integration` requiring live Postgres,
  Redis, or MinIO (`just up` provides them).
- `just test-cov` — unit tests with a coverage **floor of 90%**
  (`fail_under = 90`), measured over `src/cyberfs/domain` and
  `src/cyberfs/application` only; adapters are exercised by the integration
  suite against real backing services.

## Configuration

All configuration comes from the environment and is validated at startup —
`cyberfs.infrastructure.settings.Settings` refuses to boot on invalid config
rather than failing later. Every setting appears in
[`.env.example`](.env.example), grouped by capability:

- **Runtime** — `ENVIRONMENT`, `LOG_LEVEL`, `CORS_ALLOWED_ORIGINS`, `METRICS_ENABLED`
- **Datastores** — `DATABASE_URL`, `REDIS_URL`, `MINIO_*`
- **Identity** — `CYBERDYNE_AUTH_BASE_URL`, `CYBERFS_CLIENT_ID` /
  `CYBERFS_CLIENT_SECRET`, JWKS/discovery TTLs, `AUTH_DEV_MODE`
- **Encryption** — `MASTER_KEY`, `MASTER_KEY_PREVIOUS` (rotation),
  `ENCRYPTION_DEFAULT_ON`, `ENCRYPTION_FRAME_BYTES`, `ASYNC_REWRAP_THRESHOLD_NODES`
- **Filesystem** — quotas, upload limits, tree depth, pagination, version and
  trash retention. `TRASH_RETENTION_DAYS` bounds how long trash survives on its
  own and is what `GET /api/v1/trash` reports each entry's deadline from;
  `POST /api/v1/nodes/{id}/purge` destroys a **trashed** node sooner and
  frees its quota in the same request, and `POST /api/v1/trash/purge` does the
  same for a whole trash, bounded by `PAGE_SIZE_MAX` per call and refused with
  `409` unless the caller states the number of entries the trash actually holds. Purge is irreversible: it refuses a live
  node with `409` so losing content takes two deliberate steps, and what a
  backup taken beforehand still holds is governed by
  [the restore runbook](docs/restore-runbook.md) rather than guaranteed here.
- **Cache** — per-kind Redis TTLs, operation timeout, circuit breaker, schema version
- **Sharing / admin** — `PUBLIC_LINK_MAX_ATTEMPTS_PER_MIN`, `ADMIN_SHOW_FILENAMES`
- **Backup** — `BACKUP_ENABLED`, `BACKUP_CRON`, `BACKUP_S3_*`, retention

Guardrails enforced at boot: the dev `MASTER_KEY` placeholder is rejected in
production, `AUTH_DEV_MODE` is rejected outside local/test, and a backup target
equal to the primary MinIO endpoint and bucket is refused. `tests/unit/test_settings.py` fails if
`.env.example` and `Settings` drift apart.

## Deployment

The service ships a `Dockerfile` for local builds and a `Dockerfile.coolify` /
[`compose.coolify.yaml`](compose.coolify.yaml) stack (API, dashboard, Postgres,
Redis, MinIO) fronted by the Coolify proxy; the project descriptor and secret
list are in [`coolify.yaml`](coolify.yaml). The container entrypoint applies
Alembic migrations under a Postgres advisory lock before serving, so replicas
scale without racing the schema.

- Staging deploy and multi-replica drill: [`docs/deploy-staging.md`](docs/deploy-staging.md)
- Backup / restore procedure and `MASTER_KEY` custody: [`docs/restore-runbook.md`](docs/restore-runbook.md)
- CyberdyneAuth integration and prerequisites: [`docs/auth-integration.md`](docs/auth-integration.md)
</content>
</invoke>
