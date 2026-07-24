## 1. Foundations

- [x] 1.1 Scaffold the repository: `pyproject.toml` (Python 3.12, `uv`), `justfile` mirroring CyberdyneAuth recipes (`install`, `dev`, `test`, `test-unit`, `test-integration`, `test-cov`, `lint`, `typecheck`, `check`, `ci`), `.gitignore`, `.pre-commit-config.yaml`
- [x] 1.2 Create the hexagonal package layout `src/cyberfs/{domain,application,adapters/inbound/api,adapters/outbound,infrastructure}` with `__init__.py` and a layering guard test that fails if `domain` or `application` imports FastAPI, SQLAlchemy, redis, or minio
- [x] 1.3 Implement `infrastructure/settings.py` with Pydantic Settings covering every env var named in the specs; fail startup on missing required values, on the development `MASTER_KEY` placeholder in production, and on a `MASTER_KEY` that is not a valid 256-bit key
- [x] 1.4 Write `.env.example` covering every setting, and a test asserting it stays in sync with the settings model
- [x] 1.5 Set up structured JSON logging with `X-Request-ID` propagation and a redaction filter for secrets, tokens, and key material
- [x] 1.6 Add the FastAPI app factory, error-handler middleware mapping domain errors to RFC 7807 responses, and the `/health/live` + `/health/ready` endpoints (liveness independent of dependencies; readiness reflecting Postgres, MinIO, auth, and a `degraded` cache state)
- [x] 1.7 Add Prometheus metrics with the counters and histograms named in `deployment/spec.md`, restricted to the internal network
- [x] 1.8 Wire `.github/workflows/ci.yml`: `ruff`, `mypy --strict`, unit tests with `fail_under` above 90 on `domain`+`application`, integration tests on service containers, and `openspec validate --all --strict`
- [x] 1.9 Configure coverage in `pyproject.toml`, explicitly listing the integration-only modules excluded from the denominator with a comment justifying each

## 2. Authentication (CyberdyneAuth resource server)

- [x] 2.1 Provision the CyberFS OAuth2 client at CyberdyneAuth. Done against a local instance: client `cyb_YpkLeby8cpqxGKtp` with the `client_credentials` grant and `openid,directory:read` scopes; secret written to a mode-600 file, never printed or committed. Verified by minting a service token and introspecting it. `docs/auth-integration.md` records the contract, including the hard prerequisite that CyberdyneAuth run `JWT_ALGORITHM=RS256` + `OIDC_ENABLED=true` (both default to off). **Staging/production still need the same provisioning by an operator with admin access.**
- [x] 2.2 Define the `IdentityProvider` port in `domain/ports` and the `Principal` value object (subject, is_admin, org, orgs, is_service)
- [x] 2.3 Implement the OIDC discovery client: fetch and cache the discovery document with `OIDC_DISCOVERY_TTL_SECONDS`; never hard-code issuer, JWKS URI, or algorithm
- [x] 2.4 Implement JWKS fetching and caching with `kid`-miss refresh bounded by `JWKS_REFRESH_COOLDOWN_SECONDS`, and stale-but-usable behaviour up to `JWKS_STALE_MAX_SECONDS`
- [x] 2.5 Implement token verification: signature against discovered keys, `iss` equal to discovered issuer, `exp` with 60s skew, rejection of `alg: none` and undiscovered algorithms
- [x] 2.6 Implement the RFC 7662 introspection client with a client-credentials service token cached until 60s before expiry
- [x] 2.7 Implement the auth dependency with two modes — claim-based for ordinary reads, introspection-backed for admin actions, grants, revocations, and ownership transfer — failing closed with `503` when introspection is unreachable
- [x] 2.9 Implement `AUTH_DEV_MODE` stub principal for local development, with startup failing if it is enabled in production
- [x] 2.10 Implement per-IP rate limiting of auth failures (`RATELIMIT_AUTH_FAILURES_PER_MIN`) returning `429`
- [x] 2.11 Implement auth audit records for every `401`/`403` capturing subject, target, reason code, and source IP, with no token values
- [x] 2.12 Unit tests for every scenario in `authentication/spec.md` against a faked auth server
- [x] 2.13 Integration test against a live CyberdyneAuth (or a conformant stub) covering discovery, JWKS rotation, introspection-driven denial of a demoted admin, and cold-cache `503`

## 3. Data model and migrations

- [x] 3.1 Define domain entities: `User`, `Node` (folder/file discriminator), `FileVersion`, `Grant`, `PublicLink`, `WrappedKey`, `AuditRecord`, `QuotaUsage`
- [x] 3.2 Define repository ports in `domain/ports` for nodes, versions, grants, public links, keys, audit, quota, and the Unit of Work
- [x] 3.3 Write the SQLAlchemy 2.0 async models: adjacency-list tree with `parent_id`, partial unique index on `(parent_id, normalized_name)` for non-deleted rows, indices for owner lookups, ancestor walks, grant resolution, and trash queries
- [x] 3.4 Create the initial Alembic migration and wire migrations-on-boot with an advisory lock so concurrent replicas serialize, and non-zero exit on failure
- [x] 3.5 Implement the per-request Unit of Work with transaction boundaries owned by the application layer
- [x] 3.6 Implement the recursive-CTE ancestor/descendant queries used by permission resolution and subtree operations, bounded by `MAX_TREE_DEPTH`
- [x] 3.7 Implement first-touch user provisioning: create the local user record, root folder, KEK, and default quota on first authenticated request; refresh `org`/`orgs`/`is_admin` from claims; treat a missing `orgs` claim as no org access (moved from 2.8 — depends on the `User` entity and the key provider)

## 4. File storage — tree and CRUD

- [x] 4.1 Implement name validation and NFC normalization; reject `/`, `\`, NUL, `.`, `..`, and names outside 1–255 characters
- [x] 4.2 Implement folder create, list with deterministic ordering and cursor pagination, rename, and recursive soft delete
- [x] 4.3 Implement path derivation on read so renaming a folder rewrites no descendant rows
- [x] 4.4 Implement move with cycle detection inside the transaction, `cross_owner_move` refusal, and serialization of concurrent moves
- [x] 4.5 Implement copy of a file and of a folder subtree: new owner, fresh objects, quota charged to the copier, no grants carried over (byte duplication completed in 5.1 via the `ContentDuplicator` port)
- [x] 4.6 Implement optimistic concurrency via an `If-Match` version token returning `412` on mismatch, and `409` on concurrent same-name creation
- [x] 4.7 Implement node metadata read including the caller's effective permission and encryption state
- [x] 4.8 Implement metadata search scoped to nodes the caller owns or is granted, with no content matching
- [x] 4.9 Unit tests for tree invariants, naming, cycles, and concurrency scenarios in `file-storage/spec.md`

## 5. File storage — objects, versions, quotas

- [x] 5.1 Define the `ObjectStore` port and implement the MinIO adapter with chunked streaming put/get/delete and range reads
- [x] 5.2 Implement object key derivation `{owner_id}/{node_id}/{version_id}` containing no user-supplied text
- [x] 5.3 Implement streaming upload: bounded memory at `UPLOAD_CHUNK_BYTES`, plaintext SHA-256 computed in-flight, metadata written only after the object write succeeds, `413` above `MAX_UPLOAD_BYTES`, `400` on declared-length mismatch
- [x] 5.4 Implement streaming download with correct plaintext `Content-Length`, `Range` support returning `206`, `404` (not `403`) for callers with no grant, and digest verification raising `integrity_failure`
- [x] 5.5 Assert in code review and in a test that no endpoint ever returns a presigned MinIO URL
- [x] 5.6 Implement versioning: new version on content replace, restore-as-new-version, pruning beyond `VERSION_RETENTION_COUNT`, no version on metadata-only edits
- [x] 5.7 Implement quota accounting charged to the owner, covering live, trashed, and retained-version bytes; `507` on exceed; recipients never charged
- [x] 5.8 Implement the trash: soft delete hides from listings and search, restore to original parent or to root when the parent is gone, grants revoked on delete
- [x] 5.9 Implement the purge job for nodes past `TRASH_RETENTION_DAYS`, deleting metadata, all version objects, and all wrapped keys, and releasing quota
- [x] 5.10 Implement the orphan reaper for objects older than `ORPHAN_GRACE_MINUTES` with no referencing row, recording reclaimed bytes
- [x] 5.11 Implement the quota reconciliation job recomputing usage from metadata and correcting drift
- [x] 5.12 Integration tests against real MinIO: large streamed upload with bounded memory, interrupted upload leaving no visible file, range reads, orphan reaping, purge releasing quota

## 6. Sharing

- [x] 6.1 Implement the role value object with the total order `viewer < editor < owner` and the per-role operation matrix
- [x] 6.2 Implement effective-permission resolution as a pure domain function over ownership, direct grants, and ancestor grants, taking the maximum with no deny semantics
- [x] 6.3 Implement grant creation with recipient lookup by subject or email via CyberdyneAuth, regrant replacing the existing role, and refusal of self-grant and of grants by non-owners (CyberdyneAuth publishes no global email lookup — only the org-scoped `/orgs/{id}/members` directory — so email resolution works within the sharer's organisations; sharing by subject is unrestricted)
- [x] 6.4 Implement grant listing for owners, the "shared with me" listing returning only subtree roots, owner-initiated revocation, and recipient self-removal
- [x] 6.5 Ensure move in and out of shared folders immediately changes inherited access, and that new descendants inherit without an extra grant
- [x] 6.6 Implement public links: ≥128-bit token not encoding the node id, optional expiry, optional passphrase with rate-limited attempts, `viewer`-only, revocation, `404` when expired or revoked, and no traversal above the linked folder
- [x] 6.7 Implement ownership transfer: quota moved, `507` when the recipient cannot accommodate it, previous owner left with `editor` by default, transaction aborted if any key rewrap fails
- [x] 6.8 Implement share auditing for grants, regrants, revocations, transfers, link creation, and link use, with immutable audit records rejecting modification even by admins
- [x] 6.9 Unit tests for every inheritance and effective-permission scenario in `sharing/spec.md`, including highest-role-wins in both directions
- [x] 6.10 Integration test asserting revocation denies the very next request with no reliance on cache expiry

## 7. Content encryption

- [ ] 7.1 Define the `KeyProvider` and `ContentCipher` ports in the domain
- [ ] 7.2 Implement the key hierarchy: `MASTER_KEY` wrapping per-user KEKs, KEKs wrapping per-file DEKs; keys generated from a CSPRNG; unwrapped material never persisted or logged
- [ ] 7.3 Implement framed AES-256-GCM sealing with a unique nonce per frame and frame index plus version id bound as associated data
- [ ] 7.4 Implement streaming encryption on upload and streaming decryption on download with bounded memory, plus frame-granular range decryption
- [ ] 7.5 Implement tamper, reorder, truncation, and cross-version-substitution detection, returning `integrity_failure` and an alert-level log without leaking nonces, tags, or ciphertext
- [ ] 7.6 Implement the opt-in model: immutable per-file `encrypted` flag, folder `encryption_default` of `inherit`/`on`/`off`, nearest-ancestor resolution, `ENCRYPTION_DEFAULT_ON`, per-request override recorded in the audit log, and existing files unaffected by a later default change
- [ ] 7.7 Implement encryption-state conversion as an atomic new-version rewrite; require `owner` to decrypt; destroy plaintext objects on encrypt and wrapped keys on decrypt; audit every downgrade
- [ ] 7.8 Implement DEK rewrap on share and revoke inside the grant transaction, including folder shares rewrapping every encrypted descendant and new descendants rewrapping at creation
- [ ] 7.9 Implement the async rewrap path for large subtrees, with the grant becoming visible only on completion
- [ ] 7.10 Implement `MASTER_KEY` rotation accepting the previous and new key concurrently, rewrapping all KEKs without touching content, resumable after interruption
- [ ] 7.11 Implement per-user KEK rotation rewrapping all DEKs and destroying the old KEK on completion
- [ ] 7.12 Add readiness failure when `MASTER_KEY` is absent while encrypted files exist
- [ ] 7.13 Unit tests for the key hierarchy, framing, associated-data binding, inheritance resolution, and rotation resumability
- [ ] 7.14 Integration tests asserting MinIO objects contain no plaintext, that a revoked recipient has no wrapped DEK, and that admins receive no key material from any endpoint

## 8. Caching

- [ ] 8.1 Define the `Cache` port and implement the Redis adapter with `CACHE_OP_TIMEOUT_MS` fast timeouts, a circuit breaker tripping after `CACHE_CIRCUIT_TRIP_SECONDS`, and automatic recovery
- [ ] 8.2 Implement key naming `cyberfs:v<schema>:<dataset>:…` including the requesting subject for permission-dependent values and the cursor and page size for listings; bump-to-invalidate on schema change
- [ ] 8.3 Cache folder listings, node metadata, effective-permission decisions, discovery and JWKS documents, quota counters, and admin aggregates — and nothing else; assert audit records and grant listings are never cached
- [ ] 8.4 Implement synchronous invalidation on every mutation covering old and new parent listings, node metadata, and subtree permission decisions on grant change and on move
- [ ] 8.5 Make a write fail when invalidation is rejected by a reachable Redis
- [ ] 8.6 Apply a finite TTL to every entry, with `CACHE_TTL_PERMISSION_SECONDS` defaulting to 60 and `CACHE_TTL_JWKS_SECONDS` respecting rotation
- [ ] 8.7 Implement stampede protection coalescing concurrent misses on the same key into a single recomputation
- [ ] 8.8 Implement degraded mode: serve from Postgres, report `degraded` on readiness, never `5xx` solely because Redis is down
- [ ] 8.9 Add per-dataset cache metrics and the admin purge endpoint that reports counts and TTL distribution but never cached values
- [ ] 8.10 Add a test asserting no Redis value ever contains plaintext, ciphertext frames, key material, or bearer tokens
- [ ] 8.11 Integration tests against real Redis: cold-cache correctness, per-subject key isolation, revocation-beats-TTL, Redis-down degraded mode, and circuit recovery

## 9. Admin API

- [ ] 9.1 Implement `/api/v1/admin/**` with introspection-backed `is_admin` on every route and a test enumerating the admin router asserting no content-returning route exists
- [ ] 9.2 Implement per-user statistics: bytes used, quota, percentage, file and folder counts, encrypted vs unencrypted counts and bytes, trashed bytes, version bytes, shares granted and received, last activity, created-at
- [ ] 9.3 Implement tenant-wide statistics: totals, content-type distribution, encrypted share of storage, growth over 7/30/90 days, top consumers, active users, public-link counts
- [ ] 9.4 Implement `ADMIN_SHOW_FILENAMES` defaulting off, redacting names in cross-user listings and auditing when enabled
- [ ] 9.5 Implement quota administration: read and update any user's quota, accept lowering below current usage by marking the user over quota while permitting reads and deletes, audit previous and new values
- [ ] 9.6 Implement the sharing review surface listing active public links and allowing admin revocation, while refusing admin revocation of user-to-user grants with `403`
- [ ] 9.7 Implement the browsable audit log filterable by actor, action, target, and time range with pagination
- [ ] 9.8 Implement the operational health surface reporting dependency reachability and latency plus last run, outcome, and duration of the purge, reaper, reconciliation, and backup jobs
- [ ] 9.9 Add a test asserting an admin cannot download a file they do not own, cannot self-grant, and receives no key material from any admin response
- [ ] 9.10 Add a test asserting reported usage totals reconcile with the sum of node sizes in Postgres

## 10. Admin dashboard (SvelteKit MVVM)

- [ ] 10.1 Scaffold `admin/` with SvelteKit 2 and Svelte 5 runes, matching the CyberdyneAuth admin app's tooling and conventions
- [ ] 10.2 Implement the single typed API client module as the sole network boundary, with generated or hand-maintained types matching the admin API
- [ ] 10.3 Implement the CyberdyneAuth login flow: redirect unauthenticated visitors with return-to, transparent token refresh, redirect to login only when refresh fails, access-denied page for authenticated non-admins
- [ ] 10.4 Implement the overview route with its view model: totals, growth chart over a selectable window, top consumers, encryption adoption
- [ ] 10.5 Implement the users route with its view model: sortable and filterable list, over-quota and inactive filters, pagination
- [ ] 10.6 Implement the user detail route with its view model: live/trashed/version byte breakdown, encryption adoption, share counts, quota editing
- [ ] 10.7 Implement the sharing route with its view model: active public links and admin revocation
- [ ] 10.8 Implement the audit route with its view model: filters by actor, action, target, and time range
- [ ] 10.9 Implement the health route with its view model: dependency status, job status, degraded-cache indication
- [ ] 10.10 Add a lint rule or review-enforced check that `.svelte` files contain no direct HTTP calls and no business logic
- [ ] 10.11 Unit test every view model headlessly against a mocked API client, covering loading, error, empty, filtering, sorting, and pagination states
- [ ] 10.12 Add automated accessibility checks across every route, failing on serious or critical violations

## 11. Backup and restore

- [ ] 11.1 Implement the backup job: consistent `pg_dump` of all application tables plus `mc mirror` of the content bucket to the configured S3-compatible target
- [ ] 11.2 Implement startup validation rejecting a backup target identical to the primary MinIO endpoint and bucket
- [ ] 11.3 Implement the manifest: every object key with size and checksum, the dump checksum, and the schema migration revision
- [ ] 11.4 Implement verification confirming the dump checksum and sampling at least `BACKUP_VERIFY_SAMPLE_COUNT` objects, marking the backup failed on any mismatch and emitting an alert
- [ ] 11.5 Implement dump/mirror skew detection reported in the backup record
- [ ] 11.6 Implement scheduling on `BACKUP_CRON`, admin-triggered manual runs, overlap prevention, and clean disablement via `BACKUP_ENABLED`
- [ ] 11.7 Implement retention over `BACKUP_KEEP_DAILY`/`WEEKLY`/`MONTHLY`, never deleting the last verified backup, and pruning failed artifacts after `BACKUP_FAILED_GRACE_HOURS`
- [ ] 11.8 Implement the scripted restore: load the dump, apply migrations to the recorded revision and report the upgrade path, mirror objects into the target bucket, succeed only when readiness passes, and refuse a non-empty target without an explicit destructive flag
- [ ] 11.9 Implement backup listing by timestamp, verification state, size, and schema revision
- [ ] 11.10 Implement `key_unavailable` degradation when restoring without the correct `MASTER_KEY`, keeping unencrypted files readable and the service not-healthy
- [ ] 11.11 Add a test asserting no backup artifact contains `MASTER_KEY` in any form
- [ ] 11.12 Implement backup observability: status on the health view, staleness alert past `BACKUP_MAX_AGE_HOURS`, failure metrics and error logs, history over `BACKUP_HISTORY_DAYS`
- [ ] 11.13 Write the restore runbook documenting `MASTER_KEY` custody as a prerequisite and where it is held
- [ ] 11.14 Implement the CI backup/restore round-trip integration test: seed encrypted and unencrypted files, multiple versions, shares, and trashed nodes; back up; restore into a scratch stack; assert byte-level fidelity and that nothing is silently missing

## 12. Deployment

- [ ] 12.1 Write the multi-stage non-root `Dockerfile` for the API and `admin/Dockerfile` for the dashboard, pinned to explicit base versions and excluding tests and dev dependencies
- [ ] 12.2 Write `docker-compose.yml` for local development with Postgres, Redis, and MinIO, plus `just dev` bringing the whole stack to a serving state in one command and `just reset` wiping it
- [ ] 12.3 Implement automatic bucket provisioning at startup with private access and versioning enabled
- [ ] 12.4 Write `Dockerfile.coolify`, `compose.coolify.yaml`, and `coolify.yaml` defining API, dashboard, Postgres, Redis, and MinIO with health checks and named volumes, keeping MinIO unpublished
- [ ] 12.5 Verify the dashboard configuration carries no Postgres, Redis, or MinIO credentials
- [ ] 12.6 Verify multi-replica operation: statelessness, migration lock under concurrent start, and correct behaviour of any replica for any request
- [ ] 12.7 Document required Coolify secrets (`MASTER_KEY`, `CYBERFS_CLIENT_SECRET`, storage credentials) and confirm none are committed
- [ ] 12.8 Deploy to staging and run a full restore drill against it

## 13. Documentation and close-out

- [ ] 13.1 Write `README.md` covering the stack, quick start, `just` recipes, architecture, testing, env vars, and deployment, in the shape of the CyberdyneAuth README
- [ ] 13.2 Write `docs/architecture.md` describing the hexagonal layout and the ports and adapters map
- [ ] 13.3 Write `docs/encryption.md` covering the key hierarchy, the threat model, what encryption does and does not protect against, and rotation procedures
- [ ] 13.4 Write `docs/sharing.md` covering roles, inheritance, effective permission, and public links
- [ ] 13.5 Write `docs/api.md` or publish the OpenAPI document with a `just openapi` recipe
- [ ] 13.6 Write `docs/operations.md` covering backup, restore, key rotation, job schedules, and the health surface
- [ ] 13.7 Resolve the open questions in `design.md` — frame size, async rewrap threshold, public-link rate limits, and retention defaults — recording the chosen values and their rationale
- [ ] 13.8 Run `openspec validate --all --strict` and the full `just ci` gate, then archive the change with `openspec archive bootstrap-cyberfs`
