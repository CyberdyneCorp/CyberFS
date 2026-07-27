## Why

Cyberdyne products each reinvent file storage: ad-hoc S3 buckets, no shared permission model, no per-user quota accounting, and no consistent story for encrypting user content at rest. CyberFS provides one backend filesystem service — hierarchical folders and files, CRUD, sharing, optional per-file content encryption, and usage analytics — behind the existing CyberdyneAuth identity plane, so product teams get storage without re-solving identity, sharing, or key management.

This is a greenfield repository; the change bootstraps the entire service.

## What Changes

- **New service `CyberFS`** — Python 3.12 · FastAPI · async SQLAlchemy 2.0 · asyncpg · Alembic, in strict hexagonal (ports & adapters) layout, mirroring the CyberdyneAuth codebase conventions (`uv` · `just` · `ruff` · `mypy --strict`).
- **Hierarchical filesystem** — folders and files with create / read / update / move / rename / copy / soft-delete / restore / hard-delete. Paths are derived from parent links; names are unique per parent per owner.
- **Content storage on MinIO** — object bytes live in MinIO (S3 API); metadata lives in Postgres. All bytes stream **through the API** (chunked), never via presigned direct-to-bucket URLs, so authorization, quota accounting, and encryption apply to every byte.
- **Optional content encryption** — encryption is **opt-in per file, inherited from the parent folder, and off by default**. When enabled, content is sealed with AES-256-GCM envelope encryption: a per-file DEK wrapped by a per-user KEK, itself wrapped by a service `MASTER_KEY`. Ciphertext is what MinIO stores. Only the owner and share recipients can obtain plaintext; administrators can never read content, encrypted or not.
- **Sharing** — owners grant `viewer` / `editor` / `owner` roles on a file or folder to another CyberdyneAuth user (or to a public link with an expiry). Folder grants cascade to descendants. Sharing an encrypted item rewraps its DEK for the recipient's KEK; it never decrypts to storage.
- **Authentication via CyberdyneAuth** — CyberFS is a resource server. Access tokens are verified against CyberdyneAuth's `/.well-known/openid-configuration` (discovered `issuer`, `jwks_uri`, and signing algorithm — never hard-coded), with RFC 7662 introspection for immediate-revocation-sensitive operations. The `is_admin` claim gates the admin surface.
- **Redis cache** — caches directory listings, metadata lookups, resolved permission checks, JWKS, and quota counters, with explicit invalidation on every mutation. The cache is never the system of record and CyberFS stays correct (slower) when Redis is down.
- **Svelte admin dashboard** — SvelteKit 2 · Svelte 5 runes, MVVM (`*.vm.svelte.ts` view models separate from `.svelte` views), matching the CyberdyneAuth admin app. Admins see per-user storage consumption, file/folder counts, encryption adoption, share graphs, quota breaches, and activity trends — **metadata only, never file content**.
- **Backup & restore** — scheduled `pg_dump` of metadata plus `mc mirror` of the object bucket to an offsite S3-compatible target, with retention policy, documented restore runbook, and an integration test that performs a real restore into a scratch stack. `MASTER_KEY` is explicitly out-of-band.
- **Coolify deployment** — `Dockerfile.coolify` · `compose.coolify.yaml` · `coolify.yaml` with Postgres, Redis, and MinIO services, health checks, and migrations on boot, consistent with other Cyberdyne systems.
- **Test gates** — unit coverage floor **> 90%** enforced in CI (`fail_under`), plus integration tests against real Postgres / Redis / MinIO containers and end-to-end HTTP flows.

**BREAKING**: none — new service, no existing consumers.

## Capabilities

### New Capabilities

- `authentication`: Verifying CyberdyneAuth access tokens as a resource server (discovery-driven JWKS validation, introspection, service-to-service tokens, `is_admin` gating, principal resolution and first-touch user provisioning).
- `file-storage`: The filesystem tree itself — folder and file CRUD, hierarchy invariants, move/rename/copy, soft delete and restore, versioning of content, streaming upload/download, quotas, and the MinIO object layout.
- `sharing`: Grants of `viewer`/`editor`/`owner` on files and folders to other users or public links, inheritance down the tree, effective-permission resolution, revocation, and share listing.
- `content-encryption`: Optional envelope encryption of file content — opt-in and inheritance rules, key hierarchy (`MASTER_KEY` → user KEK → file DEK), streaming AEAD framing, DEK rewrap on share and on ownership transfer, key rotation, and the guarantee that administrators cannot read content.
- `caching`: Redis-backed caching of listings, metadata, permission decisions, JWKS and quota counters, with key naming, TTLs, invalidation triggers, stampede control, and degraded-mode behaviour.
- `admin-dashboard`: The Svelte MVVM admin app and the admin API it consumes — per-user storage usage, tenant-wide statistics, quota management, share auditing, and the hard prohibition on exposing file content to administrators.
- `backup-restore`: Scheduled metadata and object backups, retention, integrity verification, restore procedure and its automated test, plus key-material handling during backup.
- `deployment`: Runtime topology and configuration — Coolify manifests, container images, required services, environment variables, migrations on boot, health/readiness probes, and observability.

### Modified Capabilities

None — no specs exist yet in `openspec/specs/`.

## Impact

- **New repository content**: `src/cyberfs/{domain,application,adapters,infrastructure}`, `admin/` (SvelteKit), `alembic/`, `tests/{unit,integration,e2e}`, `justfile`, `pyproject.toml`, Docker and Coolify manifests, `.github/workflows/ci.yml`.
- **External dependencies**: CyberdyneAuth (OIDC discovery, JWKS, introspection, and an OAuth2 client-credentials service client that CyberFS must be provisioned with), Postgres 16, Redis 7, MinIO, and an offsite S3-compatible backup target.
- **Operational**: `MASTER_KEY` becomes critical key material — losing it means losing every encrypted file. Its custody, rotation, and out-of-band backup are a deployment prerequisite, not an implementation detail.
- **Security posture**: CyberFS holds the master key, so it is a decryption oracle for anyone who compromises both the key and the database. This is a deliberate trade-off to keep plain REST clients working and to let the server enforce quotas and stream content; it is documented in `design.md` and revisited if a zero-knowledge mode is ever requested.
