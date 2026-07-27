# caching Specification

## Purpose
TBD - created by archiving change bootstrap-cyberfs. Update Purpose after archive.
## Requirements
### Requirement: Cache is an accelerator, never the system of record

Redis SHALL hold only derived data that can be recomputed from Postgres and MinIO. Losing the entire cache SHALL cause no data loss and no incorrect authorization outcome.

#### Scenario: Cold cache serves correct results

- **WHEN** Redis is flushed and a request arrives
- **THEN** the system SHALL recompute the result from Postgres and serve it correctly

#### Scenario: Redis unavailable degrades, not fails

- **WHEN** Redis is unreachable
- **THEN** the system SHALL continue serving reads and writes from Postgres and MinIO, SHALL report `degraded` on the health endpoint, and SHALL NOT return `5xx` for that reason alone

#### Scenario: Cache never holds content or keys

- **WHEN** any value is written to Redis
- **THEN** it SHALL NOT contain file plaintext, ciphertext frames, unwrapped keys, or bearer tokens

### Requirement: Cached datasets

CyberFS SHALL cache exactly these datasets, each with its own key namespace and TTL: folder listings, node metadata, effective-permission decisions, CyberdyneAuth discovery and JWKS documents, per-user quota usage counters, and admin statistics aggregates.

#### Scenario: Listing served from cache

- **WHEN** the same folder listing page is requested twice within its TTL with no intervening mutation
- **THEN** the second request SHALL be served from Redis without querying Postgres for the node rows

#### Scenario: Permission decision cached

- **WHEN** a caller's effective permission on a node has been computed
- **THEN** it SHALL be cached keyed by subject and node, so repeated access checks in a session avoid recomputing ancestor traversal

#### Scenario: Quota counter cached

- **WHEN** a user's usage is needed for a quota check
- **THEN** it SHALL be read from the cached counter and adjusted atomically on write, with periodic reconciliation against Postgres

#### Scenario: Uncached datasets

- **WHEN** audit records or share-grant listings are requested
- **THEN** they SHALL be read from Postgres directly and SHALL NOT be cached

### Requirement: Key naming and namespacing

Cache keys SHALL be prefixed `cyberfs:v<schema>:<dataset>:` and SHALL incorporate every input that changes the result — including the requesting subject for any permission-dependent value — so that one principal's cached value can never be served to another.

#### Scenario: Per-subject isolation

- **WHEN** two different users list the same folder
- **THEN** their results SHALL occupy distinct cache keys

#### Scenario: Pagination in the key

- **WHEN** two pages of the same listing are requested
- **THEN** the cursor and page size SHALL be part of the key

#### Scenario: Schema bump invalidates wholesale

- **WHEN** the cache schema version is incremented on deploy
- **THEN** all previously cached entries SHALL become unreachable without requiring an explicit flush

### Requirement: Invalidation on mutation

Every mutation SHALL invalidate the cache entries it can affect, within the same request, before the response is returned. Correctness SHALL NOT depend on TTL expiry.

#### Scenario: Create invalidates the parent listing

- **WHEN** a node is created, deleted, renamed, or moved
- **THEN** the listings of its old and new parents SHALL be invalidated before the response is sent

#### Scenario: Metadata update invalidates the node

- **WHEN** a node's metadata or content changes
- **THEN** its cached metadata entry and its parent's listing SHALL be invalidated

#### Scenario: Grant invalidates permission decisions

- **WHEN** a grant is created, changed, or revoked on a node
- **THEN** the cached permission decisions for the affected subject over that node and its entire subtree SHALL be invalidated

#### Scenario: Revocation is not delayed by TTL

- **WHEN** a grant is revoked and the recipient immediately requests the node
- **THEN** the request SHALL be denied, regardless of any TTL still outstanding

#### Scenario: Move invalidates the subtree

- **WHEN** a folder is moved
- **THEN** cached permission decisions and listings for its descendants SHALL be invalidated

#### Scenario: Invalidation failure fails the write

- **WHEN** invalidation cannot be performed because Redis rejects the operation while reachable
- **THEN** the system SHALL fail the request rather than return success with stale authorization data cached

### Requirement: TTLs bound staleness

Every cached entry SHALL carry a finite TTL, configurable per dataset, so that any invalidation missed by a bug self-heals.

#### Scenario: Every entry expires

- **WHEN** any value is written to Redis
- **THEN** it SHALL be written with a TTL and SHALL NOT be persisted indefinitely

#### Scenario: Permission TTL is short

- **WHEN** a permission decision is cached
- **THEN** its TTL SHALL be at most `CACHE_TTL_PERMISSION_SECONDS`, defaulting to 60 seconds

#### Scenario: JWKS TTL respects rotation

- **WHEN** the JWKS is cached
- **THEN** its TTL SHALL be at most `CACHE_TTL_JWKS_SECONDS` and an unknown `kid` SHALL force an early refresh

### Requirement: Stampede and failure control

Cache misses on hot keys SHALL NOT produce a thundering herd against Postgres, and Redis faults SHALL NOT cascade into request latency.

#### Scenario: Concurrent misses coalesced

- **WHEN** many concurrent requests miss the same key
- **THEN** the system SHALL recompute the value once and serve the result to all waiters

#### Scenario: Redis operations time out fast

- **WHEN** a Redis operation exceeds `CACHE_OP_TIMEOUT_MS`
- **THEN** the system SHALL abandon it, proceed against Postgres, and record a cache-timeout metric

#### Scenario: Circuit opens on repeated failure

- **WHEN** Redis operations fail continuously for `CACHE_CIRCUIT_TRIP_SECONDS`
- **THEN** the system SHALL stop attempting cache operations for a cooldown period and serve entirely from Postgres

#### Scenario: Recovery is automatic

- **WHEN** Redis becomes reachable again after a circuit trip
- **THEN** the system SHALL resume caching without a restart

### Requirement: Cache observability

CyberFS SHALL expose per-dataset cache metrics and SHALL let administrators inspect and purge cache state without shell access to Redis.

#### Scenario: Hit ratio exposed

- **WHEN** metrics are scraped
- **THEN** hit count, miss count, eviction count, error count, and operation latency SHALL be reported per dataset

#### Scenario: Admin purge

- **WHEN** an administrator triggers a cache purge for a dataset
- **THEN** the system SHALL invalidate that namespace, record an audit entry, and SHALL NOT lose any durable data

#### Scenario: Purge does not expose values

- **WHEN** an administrator inspects cache state
- **THEN** the response SHALL report key counts, memory use, and TTL distribution, and SHALL NOT return cached values

