# CyberFS

Backend filesystem service with sharing, optional content encryption, and usage
analytics. CyberFS stores a per-user tree of folders and files, versions their
content in object storage, and exposes it over a versioned HTTP API. Identity is
delegated entirely to [CyberdyneAuth](docs/auth-integration.md) — CyberFS is an
OAuth2/OIDC **resource server**; it keeps no passwords and runs no login flow.

Highlights:

- **Hierarchical filesystem** — folders, files, rename/move, trash with
  retention, content versioning, and metadata search (`/api/v1/nodes`,
  `/api/v1/search`).
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
- **Backup & restore** — scheduled off-site backups to a separate S3 target and
  a verified restore pipeline (see [`docs/restore-runbook.md`](docs/restore-runbook.md)).

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
  trash retention
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
