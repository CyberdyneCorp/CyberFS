# Operations guide

How to run CyberFS day to day: backups, restores, key rotation, the background
maintenance jobs, and the health surface an orchestrator watches. Everything
here is driven by settings in `src/cyberfs/infrastructure/settings.py` (see
`.env.example` for the environment-variable names) and exposed through the admin
operations endpoints in
`src/cyberfs/adapters/inbound/api/routers/admin.py`.

## Backups

The backup pipeline (`src/cyberfs/application/backup.py`) takes one consistent
run: it dumps all Postgres metadata, mirrors every content object into a second
object store **as ciphertext** (`source.get()` → `target.put()`, never a
decrypt), writes a manifest of keys/sizes/checksums, verifies, and only then
marks the run successful. A verification miss or any error fails the run, and a
failed run never counts toward retention.

### Enabling and scheduling

Backups are off by default. Enable them and point them at a **separate** object
store:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BACKUP_ENABLED` | `false` | Master switch. When off, nothing is scheduled and the backup health probe reports `disabled`. |
| `BACKUP_CRON` | `0 3 * * *` | Cron expression for the scheduled run (default 03:00 daily). |
| `BACKUP_S3_ENDPOINT` | — | Backup target endpoint. |
| `BACKUP_S3_ACCESS_KEY` / `BACKUP_S3_SECRET_KEY` | — | Backup target credentials. |
| `BACKUP_S3_BUCKET` | — | Backup target bucket. |
| `BACKUP_VERIFY_SAMPLE_COUNT` | `50` | Objects re-checksummed against the manifest during verification. |
| `BACKUP_KEEP_DAILY` / `BACKUP_KEEP_WEEKLY` / `BACKUP_KEEP_MONTHLY` | `7` / `4` / `6` | Grandfather-father-son retention. |
| `BACKUP_FAILED_GRACE_HOURS` | `24` | How long a failed run is kept before pruning. |
| `BACKUP_MAX_AGE_HOURS` | `48` | Staleness threshold for the health alert (see below). |
| `BACKUP_HISTORY_DAYS` | `90` | Window the history listing and retention consider. |

When `BACKUP_ENABLED=true`, the composition root builds a `CronScheduler`
(`src/cyberfs/infrastructure/scheduler.py`) over `BACKUP_CRON`, and the app
lifespan starts it (`src/cyberfs/adapters/inbound/api/app.py`). When backups are
disabled the probe is registered in a `disabled` state and nothing is scheduled.

### Target-separation rule

The settings validator `_validate_backup_target` refuses to start if
`BACKUP_ENABLED=true` and any of `BACKUP_S3_ENDPOINT`, `BACKUP_S3_ACCESS_KEY`,
`BACKUP_S3_SECRET_KEY`, or `BACKUP_S3_BUCKET` is missing. It also refuses when
the backup target's endpoint **and** bucket both equal the primary
(`MINIO_ENDPOINT` / `MINIO_BUCKET`): a backup written into the primary store is
not a backup. Point the target at a distinct endpoint or bucket.

### Triggering a backup by hand

```
POST /api/v1/admin/operations/backup
```

Runs a backup immediately through the same procedure — and the same overlap
guard — as a scheduled run. The trigger reuses the scheduler's `trigger()`, so:

- it is refused when backups are disabled, and
- it returns `409` when a run is already in flight (the scheduler skips
  concurrent runs rather than starting two at once).

On success it returns the persisted `BackupRecordSummary` for the completed run.

### Verification and retention

Verification (`BackupService.verify`) reconfirms the dump checksum and
re-checksums `BACKUP_VERIFY_SAMPLE_COUNT` mirrored objects against the manifest;
any mismatch raises and fails the backup, so it never enters retention.

Retention (`BackupService.apply_retention`) prunes by the grandfather-father-son
policy above and **always protects the most recent verified backup**, even if
the policy would otherwise drop it. Failed runs are kept only for
`BACKUP_FAILED_GRACE_HOURS`.

Browse the history — successes and failures, newest first — with:

```
GET /api/v1/admin/operations/backups
```

## Restore

Restores are non-destructive by default and never run automatically. The full
procedure — prerequisites, scratch-stack drills, and verification — lives in
[restore-runbook.md](./restore-runbook.md).

In short:

```
just restore <backup-id>                    # non-destructive (default)
just restore <backup-id> destructive=true   # overwrite a non-empty target
```

which invokes `python -m cyberfs.restore --backup-id <uuid> [--destructive]`
(`src/cyberfs/restore.py`, `src/cyberfs/application/restore.py`). Two guards
shape it:

- **Non-destructive by default.** A target that already holds data is refused
  with a conflict unless `--destructive` is passed. A fresh database and empty
  bucket restore without the flag.
- **Key-aware degradation.** `MASTER_KEY` is never stored in any backup
  artifact. Restoring reinstates wrapped key material (KEKs, DEKs), but if the
  deployment's `MASTER_KEY` cannot open it, unencrypted files stay readable
  while encrypted ones surface a `key_unavailable` error. The restore then
  reports `healthy: False` and the `encryption` health probe stays `down`, even
  when every object was mirrored back — an encrypted stack nobody can decrypt is
  not healthy. If the key was rotated after the backup, also supply
  `MASTER_KEY_PREVIOUS`.

`pg_dump` / `pg_restore` (the `postgresql-client` package) must be installed on
the host running a restore; their absence surfaces as `backup_tool_unavailable`.

## Key rotation

`MASTER_KEY` rotation (staging `MASTER_KEY_PREVIOUS`, the re-wrap sweep, and
`ASYNC_REWRAP_THRESHOLD_NODES`) is covered in
[encryption.md](./encryption.md). The operational relevance here: keep
`MASTER_KEY` under its own out-of-band custody (a secret manager / sealed
secret), separate from the backup bucket, and back it up with the same care as
the database — losing it means losing all encrypted content, and no backup can
recover it.

## Background jobs

Three maintenance sweeps in `src/cyberfs/application/jobs.py` keep storage and
accounting honest. Each returns a result rather than logging and forgetting, so
its last run surfaces in the operations view.

| Job (`name`) | What it does | Governed by |
| --- | --- | --- |
| `purge` (`PurgeJob`) | Permanently deletes trashed nodes past their retention window — the only sweep that actually frees space — dropping their objects, grants, and wrapped keys. | `TRASH_RETENTION_DAYS` (default `30`) |
| `orphan_reaper` (`OrphanReaper`) | Deletes stored objects no metadata row references (interrupted uploads), skipping anything younger than the grace period and anything not written by CyberFS. | `ORPHAN_GRACE_MINUTES` (default `60`) |
| `reconcile_quotas` (`ReconcileQuotasJob`) | Recomputes each user's usage from the rows and corrects counter drift. | — |

### Trash entries appearing after the upgrade that added `GET /api/v1/trash`

Before the trash view landed, restoring a folder cleared `deleted_at` on that one
row and left its descendants trashed — invisible, unrecoverable, and still
charged to the owner's quota. Restore now lifts the whole deletion, but rows
stranded by an *earlier* restore are still sitting there, and the new listing
shows them. So an operator may see trash entries appear for deletions users
thought were resolved, sometimes long ago.

Nothing was created by the upgrade: these are rows that already existed and were
already occupying quota. They are now restorable or purgeable, which is a strict
improvement on invisible. The `reconcile_quotas` job recomputes usage from the
rows, so the live and trashed buckets converge on what the rows actually say
without intervention. Users who want the space back can empty their trash
(`POST /api/v1/trash/purge`), and the retention sweep destroys anything older
than `TRASH_RETENTION_DAYS` regardless.

### The scheduler

`CronScheduler` (`src/cyberfs/infrastructure/scheduler.py`) is a generic async
loop: it computes the next fire time, sleeps until then, and invokes a callback,
with **overlap prevention** — a run that fires while the previous one is still
going is skipped and recorded rather than started concurrently. A job error is
logged and never kills the loop.

The scheduler is deliberately generic and is designed to drive the purge, reaper,
and reconcile sweeps the same way it drives backups. Today the composition root
wires **only the backup job** to a running scheduler (over `BACKUP_CRON`); the
three maintenance sweeps are implemented and their last-run state is tracked, but
they are not attached to an automatic trigger in the current wiring. When run,
they behave exactly as described above.

Job state (last run time, outcome, duration, detail) is tracked by the
`JobStatusRegistry` in `src/cyberfs/application/admin.py` — the expected set is
`purge`, `orphan_reaper`, `reconcile_quotas`, `backup` — and reported through the
operations endpoint.

## Health surface

CyberFS distinguishes process liveness from dependency readiness, and within
readiness distinguishes *degraded but serving* from *not serving*
(`src/cyberfs/adapters/inbound/api/health.py`,
`src/cyberfs/domain/health.py`).

### Liveness

```
GET /health/live
```

Reflects only process health and never consults a dependency — a Postgres or
MinIO outage must not make the orchestrator restart-loop an otherwise healthy
container. Always `200 {"status": "alive", ...}` while the process is up.

### Readiness

```
GET /health/ready
```

Runs every registered dependency probe and folds the results:

- A failing **required** component (database, object store, encryption, auth)
  means `not_ready` → `503`, and the replica is removed from rotation.
- A failing **optional** component means `degraded` → still `200`. Redis is
  optional: when the cache is down, CyberFS stays correct, just slower, so
  readiness reports a `degraded` cache rather than failing.
- A `disabled` component (e.g. backups switched off) is ignored — a deliberately
  off subsystem is not a fault.

### Operations endpoint

```
GET /api/v1/admin/operations
```

The admin-facing `OperationsSummary`: the same per-component readiness view, the
background-job statuses, cache state, a totals-reconcile flag, and the backup
summary. It reports counts and metadata only — never cached values or key
material.

### Backup staleness

The backup health probe
(`BackupHealthProbe`,
`src/cyberfs/adapters/inbound/api/composition.py`) and the `BackupSummary` both
raise a **staleness alert** when backups are enabled but no `verified` backup has
completed within `BACKUP_MAX_AGE_HOURS` (default `48`). A stale backup marks that
component `down` with a detail such as `no verified backup within 48h` or
`last verified backup at … is stale`. Because the backup probe is optional, a
stale backup degrades readiness rather than pulling the replica from rotation.
The Prometheus gauge `cyberfs_backup_last_success_timestamp` (exposed on
`/metrics` when `METRICS_ENABLED=true`) carries the same signal for alerting.

### Cache administration

An operator can drop a cache dataset without a restart:

```
POST /api/v1/admin/cache/{dataset}/purge
```

It returns how many keys were removed, never what they held.
