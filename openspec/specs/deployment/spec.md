# deployment Specification

## Purpose
TBD - created by archiving change bootstrap-cyberfs. Update Purpose after archive.
## Requirements
### Requirement: Runtime topology

A CyberFS deployment SHALL consist of the API service, the admin dashboard, Postgres, Redis, and MinIO. The API SHALL be the only component that talks to Postgres, Redis, and MinIO; the dashboard SHALL reach them only through the API.

#### Scenario: Dashboard has no direct dependencies

- **WHEN** the dashboard's configuration is inspected
- **THEN** it SHALL contain no Postgres, Redis, or MinIO credentials

#### Scenario: MinIO is not publicly exposed

- **WHEN** the deployment is provisioned
- **THEN** the MinIO S3 endpoint SHALL be reachable only from the API service and SHALL NOT be published to the internet

#### Scenario: API is stateless

- **WHEN** the API is scaled to more than one replica
- **THEN** any replica SHALL serve any request correctly, with no node-local state required

### Requirement: Configuration

All configuration SHALL come from environment variables validated at startup. The service SHALL refuse to start on invalid or missing required configuration rather than failing later per request.

#### Scenario: Required variables enforced

- **WHEN** `DATABASE_URL`, `REDIS_URL`, `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET`, `CYBERDYNE_AUTH_BASE_URL`, `CYBERFS_CLIENT_ID`, `CYBERFS_CLIENT_SECRET`, or `MASTER_KEY` is missing
- **THEN** startup SHALL fail with a message naming the missing variable

#### Scenario: Insecure defaults rejected in production

- **WHEN** `ENVIRONMENT` is `production` and `MASTER_KEY` equals the development placeholder
- **THEN** startup SHALL fail

#### Scenario: Master key format validated

- **WHEN** `MASTER_KEY` is not a valid 256-bit key in the expected encoding
- **THEN** startup SHALL fail with a format error and SHALL NOT log the value

#### Scenario: Example file is complete

- **WHEN** `.env.example` is compared against the settings model
- **THEN** every configurable variable SHALL be present with a safe default or a clearly marked placeholder

#### Scenario: Secrets never logged

- **WHEN** configuration is logged at startup
- **THEN** secret values SHALL be redacted

#### Scenario: Background rewrap cadence configured

- **WHEN** `REWRAP_CRON` is set
- **THEN** the worker that completes deferred rewraps of large shared subtrees SHALL run on that schedule, and its default SHALL make a pending large share usable within a few minutes

### Requirement: Container images

CyberFS SHALL ship a production image for the API and one for the dashboard, both multi-stage, non-root, and pinned to explicit base image versions.

#### Scenario: Runs as non-root

- **WHEN** a container starts
- **THEN** its process SHALL run as an unprivileged user

#### Scenario: Build is reproducible

- **WHEN** an image is built twice from the same commit
- **THEN** dependency versions SHALL be identical, resolved from committed lockfiles

#### Scenario: Image excludes development material

- **WHEN** the production image is inspected
- **THEN** it SHALL NOT contain tests, fixtures, or development-only dependencies

### Requirement: Migrations on boot

The API SHALL apply outstanding Alembic migrations before accepting traffic, and SHALL do so safely when multiple replicas start at once.

#### Scenario: Migrations applied before serving

- **WHEN** a container starts with pending migrations
- **THEN** it SHALL apply them and only then bind its HTTP port

#### Scenario: Concurrent starts serialized

- **WHEN** several replicas start simultaneously with pending migrations
- **THEN** exactly one SHALL apply them under a lock and the others SHALL wait

#### Scenario: Failed migration blocks startup

- **WHEN** a migration fails
- **THEN** the container SHALL exit non-zero and SHALL NOT serve traffic against a half-migrated schema

### Requirement: Health and readiness

The service SHALL expose a liveness endpoint that reflects only process health and a readiness endpoint that reflects dependency health, with degraded-but-serving distinguished from not-serving.

#### Scenario: Liveness independent of dependencies

- **WHEN** Postgres is unreachable
- **THEN** the liveness endpoint SHALL still respond `200` so the orchestrator does not restart-loop the container

#### Scenario: Readiness fails without Postgres or MinIO

- **WHEN** Postgres or MinIO is unreachable
- **THEN** the readiness endpoint SHALL respond `503` and the replica SHALL be removed from rotation

#### Scenario: Redis outage is degraded, not unready

- **WHEN** only Redis is unreachable
- **THEN** readiness SHALL respond `200` with a `degraded` cache status

#### Scenario: Auth outage reflected

- **WHEN** CyberdyneAuth discovery is unreachable and no usable cached JWKS exists
- **THEN** readiness SHALL report the auth dependency as failed

### Requirement: Coolify deployment

CyberFS SHALL be deployable on Coolify using the same conventions as other Cyberdyne systems, providing `Dockerfile.coolify`, `compose.coolify.yaml`, and `coolify.yaml`.

#### Scenario: Stack defined in one compose file

- **WHEN** `compose.coolify.yaml` is applied
- **THEN** it SHALL define the API, the dashboard, Postgres, Redis, and MinIO with health checks and named volumes for Postgres and MinIO data

#### Scenario: Deploy from a clean environment

- **WHEN** the stack is deployed to a fresh Coolify project with the documented environment variables set
- **THEN** it SHALL reach a healthy state with no manual steps beyond setting those variables

#### Scenario: Volumes survive redeploy

- **WHEN** the application is redeployed
- **THEN** Postgres and MinIO data volumes SHALL persist

#### Scenario: Bucket provisioned automatically

- **WHEN** the configured MinIO bucket does not exist at startup
- **THEN** the service SHALL create it with private access and versioning enabled

#### Scenario: Secrets supplied by Coolify

- **WHEN** the deployment is configured
- **THEN** `MASTER_KEY`, `CYBERFS_CLIENT_SECRET`, and storage credentials SHALL be supplied as Coolify secrets and SHALL NOT be committed to the repository

### Requirement: Local development stack

A single documented command SHALL bring up a complete working local environment.

#### Scenario: One command to run

- **WHEN** a developer runs the documented dev recipe
- **THEN** Postgres, Redis, and MinIO SHALL start, migrations SHALL apply, the bucket SHALL be created, and the API SHALL serve locally

#### Scenario: Local stack resettable

- **WHEN** the documented reset recipe is run
- **THEN** all local data SHALL be wiped and the stack SHALL return to a clean state

#### Scenario: Development works without CyberdyneAuth

- **WHEN** `AUTH_DEV_MODE` is enabled outside production
- **THEN** a local stub principal SHALL be accepted so the stack is usable without a live auth service, and enabling it in production SHALL fail startup

### Requirement: Observability

CyberFS SHALL emit structured logs, Prometheus-compatible metrics, and request correlation identifiers.

#### Scenario: Logs are structured and correlated

- **WHEN** a request is served
- **THEN** its log lines SHALL be JSON, SHALL carry a request identifier propagated from `X-Request-ID` when supplied, and SHALL include the caller subject when authenticated

#### Scenario: Metrics exposed

- **WHEN** the metrics endpoint is scraped
- **THEN** it SHALL report request counts and latencies by route and status, bytes uploaded and downloaded, encryption operation counts and latencies, cache hit ratios, quota rejections, and background job outcomes

#### Scenario: Logs carry no sensitive data

- **WHEN** any log line is emitted
- **THEN** it SHALL NOT contain file content, file names by default, bearer tokens, or key material

#### Scenario: Metrics endpoint is not public

- **WHEN** the metrics endpoint is requested from outside the deployment network
- **THEN** it SHALL be refused

### Requirement: Continuous integration gate

CI SHALL run lint, type checking, unit tests with a coverage floor above 90 percent, and integration tests against real dependency containers, and SHALL fail the build when any gate fails.

#### Scenario: Coverage floor enforced

- **WHEN** unit test line coverage of the application and domain layers falls below 90 percent
- **THEN** CI SHALL fail

#### Scenario: Type checking strict

- **WHEN** CI runs type checking
- **THEN** it SHALL run in strict mode and SHALL fail on any error

#### Scenario: Integration tests use real services

- **WHEN** the integration job runs
- **THEN** it SHALL start real Postgres, Redis, and MinIO containers rather than mocks

#### Scenario: Specs validated

- **WHEN** CI runs
- **THEN** it SHALL execute `openspec validate --all --strict` and fail on any validation error

#### Scenario: Dashboard gates run

- **WHEN** CI runs
- **THEN** it SHALL type-check, lint, and unit-test the dashboard view models and run its accessibility checks

