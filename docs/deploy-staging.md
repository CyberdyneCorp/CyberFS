# Staging deployment and restore drill

How to deploy CyberFS to a Coolify staging environment and run a full restore
drill against it. This is the runbook for task 12.8, which cannot be executed
from the repository alone: it requires a live Coolify project and operator
access. Follow it against real staging infrastructure.

The stack is defined in [`compose.coolify.yaml`](../compose.coolify.yaml); the
Coolify project descriptor and secret list are in
[`coolify.yaml`](../coolify.yaml).

## Components

`compose.coolify.yaml` deploys five services on one internal network:

| Service     | Image / build            | Published?                     | Volume      |
| ----------- | ------------------------ | ------------------------------ | ----------- |
| `api`       | `Dockerfile.coolify`     | via Coolify proxy (ingress)    | -           |
| `dashboard` | `admin/Dockerfile`       | via Coolify proxy (ingress)    | -           |
| `postgres`  | `postgres:17.2-bookworm` | no (internal only)             | `pgdata`    |
| `redis`     | `redis:7.4-bookworm`     | no (internal only)             | `redisdata` |
| `minio`     | `minio/minio:RELEASE...` | **no host ports at all**       | `miniodata` |

MinIO is never published to the internet -- the API is the only component that
reaches it. The dashboard reaches data only through the API.

## 1. Create the Coolify project

1. Create a new project/environment named `cyberfs-staging`.
2. Add a **Docker Compose** resource pointing at `compose.coolify.yaml` in this
   repository at the branch you are staging.

## 2. Set secrets and configuration

Create these as **Coolify secrets** (never committed):

| Secret                  | How to produce it                                          |
| ----------------------- | ---------------------------------------------------------- |
| `MASTER_KEY`            | `openssl rand -base64 32` (256-bit; not the dev placeholder) |
| `CYBERFS_CLIENT_SECRET` | OAuth client secret from CyberdyneAuth for `CYBERFS_CLIENT_ID` |
| `POSTGRES_PASSWORD`     | strong random password                                     |
| `MINIO_ROOT_USER`       | MinIO access key                                           |
| `MINIO_ROOT_PASSWORD`   | MinIO secret key (strong random)                           |
| `BACKUP_S3_ACCESS_KEY`  | only if `BACKUP_ENABLED=true`                              |
| `BACKUP_S3_SECRET_KEY`  | only if `BACKUP_ENABLED=true`                              |

Set these as plain environment variables:

- `CYBERDYNE_AUTH_BASE_URL` (staging CyberdyneAuth base URL)
- `CYBERFS_CLIENT_ID` (default `cyberfs`)
- `MINIO_BUCKET` (default `cyberfs-content`)
- `PUBLIC_CYBERFS_API_URL`, `PUBLIC_CYBERDYNE_AUTH_URL`, `PUBLIC_OAUTH_PROVIDER`
- Optional: `LOG_LEVEL`, `BACKUP_ENABLED` and `BACKUP_S3_ENDPOINT` /
  `BACKUP_S3_BUCKET` when enabling off-site backups.

`ENVIRONMENT` is pinned to `production` inside the compose file, so the master
key placeholder and `AUTH_DEV_MODE` are both refused.

Register the dashboard's callback `https://<dashboard-origin>/auth/callback` in
CyberdyneAuth's redirect allowlist, and add the dashboard origin to its CORS
allowlist, or login fails at initiate.

## 3. First deploy and health verification

Deploy. Then verify, in order:

1. **Migrations applied before serving.** The API entrypoint runs
   `python -m cyberfs.infrastructure.migrate` and only then starts uvicorn. Check
   the api logs for `migrations_started` / `migrations_finished`. A migration
   failure exits the container non-zero -- it never serves a half-migrated schema.
2. **Bucket auto-provisioned.** On first boot the API creates the MinIO bucket
   with **private access and versioning enabled**
   (`MinioObjectStore.ensure_bucket`). Confirm the bucket exists and that
   versioning is `Enabled`.
3. **Readiness.** `GET /health/ready` returns `200` when Postgres and MinIO are
   reachable (Redis-only outages report `200` with a `degraded` cache status);
   it returns `503` when Postgres or MinIO is down. Liveness `GET /health/live`
   is always `200` and independent of dependencies.
4. **Dashboard.** Loads over its public URL and completes an OAuth login against
   staging CyberdyneAuth.

## 4. Multi-replica / statelessness check (task 12.6)

The API is stateless -- it keeps no node-local state; every request's data lives
in Postgres, Redis, and MinIO, all shared. Any replica serves any request.

- The `api` service has **no host port mapping** (Coolify's proxy fronts it), so
  raising `deploy.replicas` in `compose.coolify.yaml` (or scaling in Coolify)
  causes no host-port collision.
- Concurrent replica boots serialize: each runs the boot migration, which takes
  a Postgres advisory lock (`MIGRATION_LOCK_ID`). Exactly one applies pending
  migrations; the others block, then find nothing to do.

Drill: scale `api` to 2+ replicas, confirm all reach `ready`, and confirm that
requests routed to either replica behave identically (upload via one, download
via another).

## 5. Restore drill

This exercises the full backup/restore pipeline against staging. See
[`restore-runbook.md`](./restore-runbook.md) for the authoritative procedure and
the `MASTER_KEY` custody rules (the key is never in a backup; you must hold it
out of band).

1. **Enable backups** (if not already): set `BACKUP_ENABLED=true` and the
   `BACKUP_S3_*` target (which must differ from the primary MinIO endpoint and
   bucket -- startup enforces this). Trigger a backup, or wait for the scheduled
   run (`BACKUP_CRON`).
2. **Seed known data.** Upload a few files (at least one encrypted, one plain)
   and record their contents/hashes.
3. **Take a backup** and note its `backup_id`.
4. **Restore into staging.** With the correct `MASTER_KEY` in the environment,
   run the restore (destructive, since staging is the target):

   ```sh
   just restore <backup_id> destructive=true
   # => uv run python -m cyberfs.restore --backup-id <backup_id> --destructive
   ```

5. **Verify integrity.** Restore runs the sample verification
   (`BACKUP_VERIFY_SAMPLE_COUNT` objects checked ciphertext-faithfully).
   Confirm: the seeded files download with identical bytes; encrypted files
   decrypt (the `encryption` health probe stays `up`, proving the `MASTER_KEY`
   matches); `GET /health/ready` is `200`.
6. **Record the result** (backup id, restore duration, sample-verify outcome) in
   the deploy log.

---

**Task 12.8 status:** left **unchecked** in
`openspec/changes/bootstrap-cyberfs/tasks.md`. Executing it requires a live
Coolify environment and operator access, which are outside the repository. Run
this runbook against staging to complete it.
