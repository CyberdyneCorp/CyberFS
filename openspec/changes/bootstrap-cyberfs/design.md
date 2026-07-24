## Context

CyberFS is a greenfield service. The repository contains only a README, so every architectural decision is open — but not unconstrained: CyberFS is a Cyberdyne system and must fit the platform its siblings already occupy.

The reference sibling is **CyberdyneAuth** (Python 3.12 · FastAPI · async SQLAlchemy 2.0 · asyncpg · Alembic · Pydantic 2 · strict hexagonal layering · SvelteKit 2 / Svelte 5 admin app in MVVM · `uv` + `just` + `ruff` + `mypy --strict` · Coolify deployment). CyberFS adopts the same stack and the same layout, so that a developer moving between the two repositories finds the same shape, and so that the deployment story is already understood by the platform.

CyberdyneAuth is also CyberFS's identity plane. It is an OIDC provider with discovery, JWKS, RFC 7662 introspection, client-credentials service tokens, and an `is_admin` claim. Its own documentation is emphatic that resource servers must derive `iss`, `jwks_uri`, and the signing algorithm from discovery rather than hard-coding them — a past incident (#47/#114) broke relying parties that hard-coded the issuer. CyberFS follows that rule.

Constraints fixed before design began:

- Content bytes stream **through the API**. No presigned direct-to-MinIO URLs. This is a prerequisite for server-side encryption, quota enforcement on every byte, and uniform authorization.
- Encryption is **optional** — per file, inherited from the parent folder, off by default.
- Administrators must be able to see usage statistics and never file content.
- Unit coverage above 90 percent, plus integration tests against real dependencies.
- Deployable on Coolify like the rest of the estate.

## Goals / Non-Goals

**Goals:**

- One backend that any Cyberdyne product can use for user file storage, with a permission model richer than "bucket per tenant".
- A sharing model that is simple to reason about: three ordered roles, inheritance down the tree, highest-role-wins.
- Encryption that costs nothing when unused and is genuinely enforced when used — including the property that an administrator, holding admin rights alone, cannot read content.
- Correctness that does not depend on the cache. Redis makes CyberFS fast; Postgres makes it right.
- A restore that has actually been executed, in CI, against real containers — not a documented aspiration.
- A codebase where the domain logic is testable without FastAPI, Postgres, Redis, or MinIO present.

**Non-Goals:**

- Zero-knowledge / end-to-end encryption. Explicitly deferred; see the decision below.
- Full-text or content-based search. Metadata search only — indexing content would require holding plaintext.
- Real-time collaborative editing, file locking, or operational transforms.
- WebDAV, FUSE, or any protocol surface beyond REST. **Superseded for S3:** the
  `add-s3-and-activity` change reverses this for an S3-compatible surface, on the
  grounds that the permission and encryption model this change exists to
  establish is now built and tested, and the cost of having no interface that
  existing tooling speaks is paid by every consuming team. WebDAV and FUSE
  remain out of scope.
- Cross-organisation federation or multi-region replication.
- Serving as a CDN. Content always transits the API; deployments that need edge caching should front CyberFS with one.
- Virus scanning, DLP, and content classification.

## Decisions

### Server-side envelope encryption, not end-to-end

**Decision.** A per-file 256-bit DEK encrypts content with AES-256-GCM. The DEK is wrapped under a per-user KEK; the KEK is wrapped under a deployment `MASTER_KEY`. Unwrapped keys live only in process memory, for the duration of a request.

**Why.** The data path was fixed as "stream through the API", which by construction means the server touches plaintext. Given that, envelope encryption is what remains, and it buys real properties: MinIO compromise alone yields nothing; a stolen database yields nothing without `MASTER_KEY`; sharing and revocation are key operations rather than data rewrites; `MASTER_KEY` can be rotated without touching a single content object.

**Alternatives considered.**

- *Zero-knowledge E2E* — the server never holds plaintext; the client wraps the DEK to the recipient's public key. Strictly stronger, and rejected for four reasons: it is incompatible with streaming through the API; it requires a crypto-capable client, so `curl` and server-to-server callers cannot upload; CyberdyneAuth supports OAuth and EVM-wallet login, so many users have no password from which to derive a private key; and it needs a key-recovery story where losing a passphrase means losing every file. It remains the natural next step if a product needs it, and the key hierarchy is deliberately shaped so an E2E vault mode could be added: an E2E file is one whose DEK is wrapped to a user-held public key rather than to the KEK.
- *MinIO server-side encryption (SSE-KMS)* — MinIO manages keys itself. Rejected: the unit of protection is the bucket or object, not the user, so it cannot express "this DEK is readable by the owner and these three recipients", and sharing would have no key-level meaning.
- *Per-user keypairs stored server-side, unlocked by password* — the hybrid. Rejected for the same OAuth/wallet reason as E2E: there is no password to derive from.

**The honest trade-off.** An attacker who obtains both `MASTER_KEY` and the database can decrypt everything. CyberFS is not a zero-knowledge system and the proposal says so plainly. What encryption buys is defence against storage compromise, backup exfiltration, and operator curiosity — not defence against full compromise of a running deployment.

### Optional encryption, inherited from the folder

**Decision.** Files carry an immutable-at-creation `encrypted` flag; folders carry an `encryption_default` of `inherit` / `on` / `off`; a file with no explicit choice resolves the nearest ancestor's setting; the root default is `off` unless `ENCRYPTION_DEFAULT_ON` is set. Changing an existing file's state is an explicit operation that rewrites content into a new version, and decryption requires `owner`.

**Why.** Most content does not need encryption, and encrypting it costs CPU, forbids MinIO-side range reads, and complicates operations. But an "encrypt this file" checkbox alone is a footgun: users forget. Folder inheritance lets a user or a product designate a subtree as sensitive once and have everything landing in it protected by default.

**Alternatives considered.** All-or-nothing per deployment — simpler, but forces the choice on the operator rather than the data owner. Per-request only, no inheritance — simplest, but every client must remember to ask, and one forgetful upload silently lands in the clear.

**Consequence taken seriously.** Because plaintext and ciphertext files coexist, `encrypted` is part of the node's public metadata, and downgrading is audited. A silent downgrade would be the worst outcome, so the spec forbids it.

### Ordered roles with tree inheritance and highest-role-wins

**Decision.** `viewer` < `editor` < `owner`. A grant on a folder reaches every descendant. Effective permission is the maximum over ownership, direct grants, and ancestor grants. There is no deny rule.

**Why.** Deny rules make effective permission non-monotonic, which is where permission systems become unexplainable to users and untestable for developers. With max-only semantics, "why can Bob read this?" is answered by walking to the root and taking the highest grant found — a single readable query, and a cache entry that can be invalidated by subtree.

**Alternatives considered.** ACLs with explicit deny — more expressive, but the interaction of inherited allow and direct deny is exactly the part users get wrong. Per-node grants with no inheritance — trivial to reason about, but sharing a folder would require materialising a grant per descendant and reconciling on every create.

**Consequence.** Moving a node changes who can see it. That is inherent to inheritance, so the spec makes it explicit in both directions (moving out revokes, moving in grants), and `cross_owner_move` is refused by default so a move cannot silently transfer cost.

### Adjacency-list tree with derived paths

**Decision.** Each node stores `parent_id`; paths are computed on read, not stored. Ancestor walks use a recursive CTE. Cycle prevention is enforced at move time inside the transaction.

**Why.** Materialised paths make rename O(subtree) with a write amplification that turns "rename a top folder" into a multi-second write storm. Closure tables make the tree cheap to query and expensive to mutate, with a table that grows quadratically in depth. Adjacency lists with recursive CTEs are cheap to mutate and adequate to query at the depths real filesystems reach, and Postgres executes them well. Effective-permission resolution — the one hot ancestor walk — is cached in Redis, which removes the main argument for a closure table.

### Cache as accelerator only, with write-path invalidation

**Decision.** Redis caches listings, node metadata, permission decisions, JWKS, quota counters, and admin aggregates. Every mutation invalidates what it can affect, synchronously, before responding. Every entry also carries a TTL as a backstop. Permission TTL is short (60s default). If Redis is reachable but rejects an invalidation, the write fails.

**Why.** The dangerous failure mode of a permission cache is serving a revoked grant. TTL-only expiry makes revocation eventually-consistent, which is not acceptable for an authorization decision — so revocation invalidates synchronously and the TTL exists only to self-heal a missed invalidation from a future bug. Failing the write when invalidation fails is deliberate: succeeding would leave a stale allow cached with no one aware.

**Alternatives considered.** Pub/sub invalidation fanned out to replicas — needed only if caches were process-local; a shared Redis makes invalidation a single operation. Write-through caching — more machinery, and it makes the cache authoritative-ish, which is precisely what this design refuses.

**Degraded mode.** Redis down means slower, not broken: circuit breaker, fast timeouts, straight to Postgres, `degraded` on the health endpoint, readiness still `200`.

### Discovery-driven auth, introspection where freshness matters

**Decision.** Verify tokens against the discovered `issuer`, `jwks_uri`, and algorithm list, caching both documents. Use RFC 7662 introspection — not the JWT claim — for admin actions, grants, revocations, and ownership transfer. Fail closed if introspection is unreachable during those operations.

**Why.** This is CyberdyneAuth's documented contract, and the reason for it is a real outage. The split between claim-based and introspection-based authorization is a latency/freshness trade: an ordinary download can tolerate an access-token-lifetime-stale `is_admin`, an admin action or a permission change cannot.

### Hexagonal layering

**Decision.**

```
src/cyberfs/
  domain/          # entities, value objects, invariants, port protocols — no I/O, no framework
  application/     # use cases orchestrating ports; transaction boundary; no FastAPI, no SQLAlchemy
  adapters/
    inbound/api/   # FastAPI routers, Pydantic schemas, DI wiring, streaming request/response handling
    outbound/      # SQLAlchemy repositories, MinIO object store, Redis cache, CyberdyneAuth client, crypto provider
  infrastructure/  # Settings, engine/session, Alembic, logging, metrics
```

Unit of Work per request; ports are `Protocol` classes owned by the domain; adapters depend inward only.

**Why.** It matches CyberdyneAuth, and it is what makes the 90 percent coverage floor achievable honestly rather than by testing getters: sharing rules, inheritance resolution, quota arithmetic, encryption-state inheritance, and cycle detection are pure functions over in-memory objects. The integration suite then covers the adapters, where real Postgres/Redis/MinIO behaviour is the thing under test.

**Coverage measurement.** The floor applies to `domain/` and `application/`. Genuinely integration-only modules (MinIO streaming, the backup runner, the Redis client wrapper) are excluded from the denominator and covered by integration tests instead — the same convention CyberdyneAuth uses. Excluding them is stated in `pyproject.toml`, not left implicit.

### Streaming with framed AEAD

**Decision.** Content is chunked into fixed-size frames. Each frame is sealed with AES-256-GCM under a unique nonce, with the frame index and the version id bound as associated data. Upload and download both stream; neither buffers a whole object.

**Why.** A single GCM seal over a whole file cannot be streamed on read without buffering, and cannot serve ranges at all. Framing gives streaming decryption, range reads by frame, and — because index and version id are authenticated — detection of frame reordering, truncation, and cross-version substitution, which naive per-chunk encryption misses.

**Trade-off.** Ciphertext is larger than plaintext by one nonce and one tag per frame, and `Content-Length` for an encrypted download is the recorded plaintext size rather than the object size. Frame size is a tunable balancing per-frame overhead against range granularity.

### Object keys carry no user input

**Decision.** Keys are `{owner_id}/{node_id}/{version_id}`. Names and paths never appear in a key.

**Why.** It removes an entire class of traversal and injection bugs, makes rename and move pure metadata operations, and makes the orphan reaper's job decidable — an object whose `node_id`/`version_id` has no row is garbage.

### Backups: dump plus mirror, verified, restore tested in CI

**Decision.** `pg_dump` at a consistent snapshot plus `mc mirror` of the bucket to a distinct S3-compatible target; a manifest with checksums and the schema revision; verification before a backup counts as successful; retention that never deletes the last verified backup; a scripted restore that refuses a non-empty target without an explicit destructive flag; and an integration test that performs a real round trip and asserts byte-level fidelity including encrypted files.

**Why.** Untested backups are approximately as valuable as no backups. The round-trip test is the requirement that makes the rest real.

**`MASTER_KEY` is deliberately excluded from backups.** A backup containing both the ciphertext and the key that opens it is a plaintext backup with extra steps. The trade is that key custody becomes an operational responsibility, stated as such in the runbook: lose the key and the encrypted files are gone.

### Admin surface is metadata-only, by construction

**Decision.** There is no admin route that returns content. Admin views are built from aggregates. File names are omitted from cross-user admin listings unless `ADMIN_SHOW_FILENAMES` is enabled, and enabling it is audited. Admins cannot grant themselves access to nodes they do not own.

**Why.** "Admins can see stats but not content" is only credible if content is unreachable from the admin surface rather than merely not linked in the UI. Names are treated as content-adjacent because `Q3 layoffs - final.xlsx` leaks the thing the user wanted private.

### Dashboard in MVVM

**Decision.** SvelteKit 2 / Svelte 5 runes. Each route has a `*.vm.svelte.ts` view model holding state, loading, filtering, sorting, pagination, and error handling; `.svelte` files are presentation only; a single typed API client is the sole network boundary. View models are unit-tested headlessly.

**Why.** Same structure as the CyberdyneAuth admin app, and it makes dashboard logic testable without a DOM — which is where the interesting bugs (pagination, aggregation, over-quota classification) actually live.

## Risks / Trade-offs

- **`MASTER_KEY` compromise decrypts everything** → Key never enters backups, logs, metrics, or admin responses; rotation is supported without rewriting content; production startup rejects the development placeholder; custody documented as a deployment prerequisite. Residual risk is accepted and stated in the proposal.
- **`MASTER_KEY` loss destroys encrypted content irrecoverably** → Out-of-band backup of the key is a documented prerequisite; restore without it degrades explicitly (`key_unavailable`) instead of appearing to succeed; readiness fails rather than serving per-file 500s.
- **The API is in the bandwidth path for every byte** → Streaming with bounded per-request memory; horizontal scaling of a stateless API; frame size tuned for throughput. If a product needs CDN-scale reads, that is a front-door concern, not a CyberFS one.
- **Stale permission cache could serve a revoked grant** → Synchronous subtree invalidation on every grant change; short TTL backstop; writes fail if invalidation fails while Redis is reachable; an integration test asserts that revocation takes effect on the very next request.
- **Recursive CTE ancestor walks degrade on deep trees** → Permission decisions and listings are cached; depth is bounded by `MAX_TREE_DEPTH`; if profiling shows this is the bottleneck, a closure table can be added behind the existing repository port without touching the domain.
- **Rewrapping DEKs when sharing a large folder is O(files)** → Done in one transaction with batched updates; large subtrees rewrap asynchronously with the grant visible only on completion, so a partially rewrapped share never appears usable.
- **Quota counters in Redis drift from reality** → Counters are an accelerator; a reconciliation job recomputes from Postgres on a schedule and corrects drift; the admin view is required to reconcile with metadata rather than display drift indefinitely.
- **Backup and dump can skew** (objects written between snapshot and mirror) → Manifest records the schema revision and object inventory; skew beyond the tolerated in-flight window is reported in the backup record rather than silently accepted.
- **A 90 percent floor invites coverage theatre** → The floor applies to domain and application layers where the logic is; integration-only modules are excluded explicitly and covered by real-dependency tests; the restore round trip and revocation-takes-effect tests are named requirements, not optional extras.
- **Divergence from CyberdyneAuth conventions over time** → Same stack, same layering, same tooling, same deployment shape; token verification follows the published contract so shared-claim changes do not require a flag day.

## Migration Plan

There is nothing to migrate from — this is a new service. What matters is the order of standing it up:

1. **Foundations** — repository skeleton, `pyproject.toml`, `justfile`, hexagonal package layout, settings, logging, health endpoints, CI with the gates wired but nothing to gate yet.
2. **Identity** — CyberdyneAuth resource-server integration. Provision the CyberFS OAuth2 client at CyberdyneAuth first; this is an external prerequisite and blocks everything authenticated.
3. **Tree and storage** — schema, migrations, node CRUD, streaming upload/download to MinIO, quotas, versions, trash and purge. Encryption off throughout.
4. **Sharing** — grants, inheritance, effective permission, public links, ownership transfer.
5. **Encryption** — key hierarchy, framed AEAD, opt-in and inheritance, rewrap on share, rotation. Layered on top of a working unencrypted path so the encryption tests can diff against known-good behaviour.
6. **Cache** — Redis for listings, metadata, permissions, quotas, JWKS, with invalidation and degraded mode.
7. **Admin API and dashboard** — aggregates, then the SvelteKit MVVM app.
8. **Backup and restore** — jobs, manifest, verification, retention, scripted restore, CI round trip.
9. **Deployment** — Coolify manifests, staging deploy, restore drill against staging.

**Rollback.** Before there are users, rollback is redeploying the previous image; migrations are written to be backward compatible for one release so a rolled-back API meets a schema it understands. Once real data exists, the restore procedure is the rollback of last resort, which is why it is tested in CI from the start.

## Open Questions

- **Org scoping.** CyberdyneAuth tokens carry `org` and `orgs`. This design keeps ownership per-user and treats orgs as metadata only. Should a folder be ownable by an organisation, with membership implying access? Deferred — it would add a second principal type to the permission model.
- **Entitlement-driven quotas.** CyberdyneAuth exposes `entitlements`. Should the default quota be derived from a user's plan rather than a single `DEFAULT_QUOTA_BYTES`? The hook exists; the mapping is unspecified.
- **Frame size.** The AEAD frame size trades per-frame overhead against range granularity and memory per request. Pick empirically during implementation and record the measurement.
- **Async rewrap threshold.** Above what subtree size does share-rewrap move from synchronous to background? Needs a number from real timings.
- **Public-link rate limiting.** Per-token and per-IP limits for passphrase attempts and downloads are required by the spec but the numbers are unset.
- **Retention defaults.** `TRASH_RETENTION_DAYS`, `VERSION_RETENTION_COUNT`, and the backup retention tiers need product sign-off, not just engineering defaults.
