# CyberFS Architecture

CyberFS is a hexagonal (ports-and-adapters) backend. Business rules — sharing,
tree inheritance, quota arithmetic, encryption-state inheritance, cycle
detection — live in a pure core that knows nothing about Postgres, Redis, MinIO,
CyberdyneAuth, or FastAPI. Everything technology-specific sits on the outside and
is reached only through interfaces the core owns.

## The four layers

Under `src/cyberfs/`:

| Layer | Path | What lives here | May import |
|---|---|---|---|
| Domain | `domain/` | Entities, value objects, invariants, and the port protocols | Nothing inward of itself |
| Application | `application/` | Use-case services that orchestrate the domain and drive ports | `domain/` only |
| Adapters | `adapters/inbound/`, `adapters/outbound/` | Concrete implementations of ports and the delivery mechanism | Any layer |
| Infrastructure | `infrastructure/` | Process-level plumbing: settings, DB engine, logging, metrics, scheduler | Any layer |

The domain is the centre. `domain/` holds the entities (`nodes.py`, `users.py`,
`sharing.py`, `keys.py`, `links.py`, `permissions.py`, `audit.py`, `backup.py`,
`framing.py`, `health.py`) and, in `domain/ports/`, the protocols that describe
what the core needs from the outside world. Nothing in `domain/` reaches out to
the application or adapter layers.

The application layer (`application/`) contains the use-case services —
`nodes.py`, `content.py`, `sharing.py`, `encryption.py`, `authentication.py`,
`provisioning.py`, `caching.py`, `admin.py`, `backup.py`, `restore.py`,
`jobs.py`, `streaming.py`, `health.py`. Each takes ports as constructor
arguments and coordinates domain objects across a transaction. It talks to
`domain.ports.*` and never to a concrete adapter.

Adapters are split by direction:

- **Inbound** (`adapters/inbound/api/`): the FastAPI delivery mechanism —
  routers, request/response schemas, dependencies, middleware, error handlers,
  and the composition root.
- **Outbound** (`adapters/outbound/`): concrete implementations of the ports —
  the SQLAlchemy repositories (`db/`), the MinIO object store (`objects/`), the
  Redis cache (`cache/`), the CyberdyneAuth clients (`auth/`), the AES-256-GCM
  crypto (`crypto.py`, `cipher.py`), the `pg_dump` backup adapter (`backup/`),
  and the audit sink (`audit_log.py`).

Infrastructure (`infrastructure/`) holds process-level concerns that are neither
domain logic nor a port implementation: `settings.py` (Pydantic settings loaded
from the environment), `db.py` (async engine and session factory), `logging.py`,
`metrics.py`, `migrate.py`, and `scheduler.py` (the cron scheduler that drives
backups).

## The dependency rule, and how it is enforced

Source dependencies point inward only. The domain depends on nothing; the
application depends on the domain; adapters and infrastructure depend on both,
but the core never depends on them. Concretely:

1. The pure layers (`domain/` and `application/`) import no web framework and no
   I/O library.
2. The domain imports neither the application nor any adapter.
3. The application imports the ports, never a concrete adapter.

These are not conventions — they are executable tests in
`tests/unit/test_layering.py`, which parses every module's imports with `ast`:

- `test_pure_layers_import_no_frameworks` fails if any module under `domain/` or
  `application/` imports `fastapi`, `starlette`, `sqlalchemy`, `asyncpg`,
  `alembic`, `redis`, `minio`, `httpx`, `uvicorn`, `prometheus_client`, or
  `cryptography`. (Crypto primitives are deliberately on that list: sealing
  belongs beside the key provider in an adapter, not inside a use case.)
- `test_domain_does_not_import_application_or_adapters` fails if any `domain/`
  module imports `cyberfs.application.*` or `cyberfs.adapters.*`.
- `test_application_does_not_import_adapters` fails if any `application/` module
  imports `cyberfs.adapters.*`.
- `test_layer_directories_exist` asserts the layer directories are present.

Keeping the core free of I/O is what makes its logic unit-testable without
Postgres, Redis, MinIO, or FastAPI running, which is what keeps the coverage
floor honest.

## Ports and their adapters

Every port is a `Protocol` under `src/cyberfs/domain/ports/`. Each is
implemented by one or more concrete outbound adapters, selected and constructed
at the composition root.

### Persistence — `domain/ports/repositories.py`

The repository protocols express *what the tree needs* (ancestor walks, subtree
reads, sibling-name lookups) rather than exposing a query builder. All are
implemented against async SQLAlchemy in `adapters/outbound/db/`:

| Port | Concrete adapter |
|---|---|
| `UserRepository` | `SqlUserRepository` (`db/repositories.py`) |
| `NodeRepository` | `SqlNodeRepository` (`db/repositories.py`) |
| `FileVersionRepository` | `SqlFileVersionRepository` (`db/repositories.py`) |
| `GrantRepository` | `SqlGrantRepository` (`db/repositories.py`) |
| `PublicLinkRepository` | `SqlPublicLinkRepository` (`db/repositories.py`) |
| `KeyRepository` | `SqlKeyRepository` (`db/repositories.py`) |
| `QuotaRepository` | `SqlQuotaRepository` (`db/repositories.py`) |
| `AuditRepository` | `SqlAuditRepository` (`db/repositories.py`) |
| `AdminQueries` | `SqlAdminQueries` (`db/admin_queries.py`) |
| `UnitOfWork` | `SqlUnitOfWork` (`db/unit_of_work.py`) |

`UnitOfWork` is the transaction boundary the application layer owns. It exposes
the repositories as attributes (`uow.users`, `uow.nodes`, `uow.grants`, …) so a
use case's writes — a grant and its key rewrap, a purge and its quota release —
commit or roll back together.

### Object storage — `domain/ports/storage.py`

`ObjectStore` streams content bytes (nothing is held in memory whole).
Implemented by `MinioObjectStore` (`adapters/outbound/objects/minio_store.py`).
The same port is reused for the backup mirror: `build_backup_object_store`
constructs a second `MinioObjectStore` pointed at the `BACKUP_S3_*` target.

### Cryptography — `domain/ports/crypto.py`

Two ports for the envelope scheme (`MASTER_KEY` → per-user KEK → per-file DEK):

| Port | Concrete adapter |
|---|---|
| `KeyProvider` (wrap/unwrap KEKs and DEKs) | `MasterKeyProvider` (`adapters/outbound/crypto.py`) |
| `ContentCipher` (seal/open framed content) | `AesGcmContentCipher` (`adapters/outbound/cipher.py`) |

`MasterKeyProvider` is built from `MASTER_KEY` and, when present,
`MASTER_KEY_PREVIOUS`, so both keys are accepted while a rotation is in flight.

### Identity — `domain/ports/identity.py`

Three deliberately separate capabilities against CyberdyneAuth:

| Port | Concrete adapter(s) |
|---|---|
| `TokenVerifier` (cheap, local, claim-based) | `JwtTokenVerifier` (`auth/verifier.py`); `DevModeVerifier` (`auth/dev_mode.py`) under `AUTH_DEV_MODE` |
| `TokenIntrospector` (RFC 7662, authoritative now) | `TokenIntrospectionClient` (`auth/introspection.py`); `DevModeVerifier` under `AUTH_DEV_MODE` |
| `UserDirectory` (resolve a share recipient to a subject) | `CyberdyneDirectory` (`auth/directory.py`); `LocalOnlyDirectory` (in `composition.py`) under `AUTH_DEV_MODE` |

The verifier/introspector split is what stops a revocation-sensitive route
(grants, ownership transfer, admin actions) from silently being written against
the cheap check. Discovery, JWKS, and service-token acquisition are handled by
`auth/discovery.py` and `auth/service_token.py`.

### Cache — `domain/ports/cache.py`

`Cache` is a narrow accelerator (string keys, JSON values, explicit TTLs, prefix
deletion) whose every method may fail without breaking correctness. Implemented
by `RedisCache` (`adapters/outbound/cache/redis_cache.py`); a `NullCache` in the
same package turns every read into a miss when Redis is not configured.

### Audit — `domain/ports/audit.py`

`AuditSink` (append-only) is implemented by `LoggingAuditSink`
(`adapters/outbound/audit_log.py`). Durable, queryable audit history is a
separate concern served by the `AuditRepository` port above.

### Backup — `domain/ports/backup.py`

| Port | Concrete adapter |
|---|---|
| `MetadataDump` (consistent Postgres dump/restore) | `PgDumpMetadataDump` (`adapters/outbound/backup/pg_dump.py`) |
| `BackupRepository` (durable backup history) | `SqlBackupRepository` (`db/backup_repository.py`) |
| `BinarySink` (streaming dump write target) | provided by the backup pipeline |

`MetadataDump` keeps `pg_dump`/`pg_restore` subprocess and DSN handling out of
the application layer. The backup *target* reuses the `ObjectStore` port, so
mirroring is a streamed `get()` → `put()` that copies ciphertext verbatim and
never decrypts anything.

### Health — `domain/ports/health.py`

`HealthProbe` reports whether a dependency is reachable. The concrete probes
live in `composition.py`: `DatabaseHealthProbe`, `AuthHealthProbe`,
`ObjectStoreHealthProbe`, `EncryptionHealthProbe`, `CacheHealthProbe`, and
`BackupHealthProbe`. Each registers itself with `HealthService`
(`application/health.py`) as its adapter comes online.

## Request lifecycle

A typical authenticated request flows strictly inward and back out:

1. **Inbound API.** A router under
   `adapters/inbound/api/routers/` (`nodes.py`, `content.py`, `shares.py`,
   `admin.py`) receives the request. Routers are thin: parse, delegate, and
   serialize — nothing more. Authorization, invariants, and transaction
   boundaries all live inward of the handler.
2. **Dependencies.** FastAPI dependencies in
   `adapters/inbound/api/dependencies.py` resolve the caller and the
   transaction. `CurrentPrincipal` (claim-based), `FreshPrincipal`
   (introspection-backed), and `AdminPrincipal` (introspection-backed +
   `is_admin`) map to the three authentication modes; `current_user` provisions
   the caller on first touch. `unit_of_work` opens one `UnitOfWork` per request
   and yields it; anything left uncommitted rolls back, so a handler that forgets
   cannot half-save.
3. **Application service.** The handler pulls the relevant use-case service off
   `request.app.state` (e.g. `app.state.nodes`, `app.state.content`,
   `app.state.sharing`) and calls it, passing the `UnitOfWork` and the resolved
   user. The service runs the domain logic.
4. **Ports.** The service reaches the outside world only through the port
   protocols — `uow.nodes`, `uow.grants`, the `ObjectStore`, the `Cache`, the
   `KeyProvider`/`ContentCipher`, the `UserDirectory`.
5. **Outbound adapters.** Those ports are the concrete adapters wired at startup
   — `SqlNodeRepository` issuing SQL, `MinioObjectStore` streaming bytes,
   `RedisCache` reading from Redis, `AesGcmContentCipher` sealing frames.
6. **Back out.** Results return through the service to the handler, which
   serializes them into the response schemas in
   `adapters/inbound/api/schemas.py`.

The dependency direction is inward at every hop: the router depends on the
service, the service depends on ports, and only the composition root knows which
concrete adapter satisfies each port.

## Composition root

Wiring is confined to two modules under `adapters/inbound/api/`:

- **`app.py`** — `create_app()` is the FastAPI application factory and the
  composition root. It loads `Settings`, creates the async engine and session
  factory, constructs every service and adapter, stashes them on
  `app.state.*`, registers each health probe, adds middleware
  (`RequestContextMiddleware`, optional metrics and CORS), registers the error
  handlers, and includes the routers. The `lifespan` context manager provisions
  the content bucket, starts and stops the backup scheduler, and disposes of the
  HTTP client and engine on shutdown.
- **`composition.py`** — the adapter factory. The `build_*` functions
  (`build_identity`, `build_object_store`, `build_key_provider`,
  `build_encryption`, `build_content`, `build_cache`, `build_sharing`,
  `build_backup`, …) construct the concrete outbound adapters from `Settings`
  and hand them to the application services as ports. This is the only place
  that names concrete adapters, and the only place environment-driven choices
  are made — for example, selecting `DevModeVerifier`/`LocalOnlyDirectory` under
  `AUTH_DEV_MODE`, or returning no backup wiring when `BACKUP_ENABLED` is false.

Because construction is centralized here, the rest of the codebase depends only
on protocols; swapping an adapter (a different object store, a different cache)
is a change to `composition.py`, not to any use case.

## Generating the API surface

The OpenAPI document is produced from the wired app, so it always reflects the
real routers:

```
just openapi
```

which runs `create_app().openapi()` and prints the schema as JSON.
