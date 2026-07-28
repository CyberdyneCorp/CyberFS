# Restore runbook

How to restore CyberFS from a backup, into production or a scratch stack.

Backups are produced by the backup job described in
`openspec/changes/bootstrap-cyberfs/specs/backup-restore/spec.md`. A backup is a
consistent Postgres dump of all metadata plus a ciphertext-faithful mirror of the
MinIO content bucket, described by a manifest. Restoring one reconstitutes the
service — **provided you also hold the deployment `MASTER_KEY`**.

## Prerequisite: `MASTER_KEY` custody

`MASTER_KEY` is **never** written into any backup artifact — not the manifest,
not the Postgres dump, not object metadata, in plain or encrypted form. This is a
hard invariant of the backup pipeline, verified by
`tests/unit/test_no_master_key_in_backup.py`.

Consequences:

- **The key is held out of band**, in the deployment's secret manager / sealed
  secret store (the same place the running service reads `MASTER_KEY` from at
  boot). It is *not* in the backup bucket, and restoring a backup does not
  recover it.
- **Without the correct `MASTER_KEY`, the backup is useless for encrypted
  content.** Unencrypted files restore and read normally; encrypted files fail
  with a clear `key_unavailable` error and the restored service reports itself
  **not healthy** (the `encryption` health probe stays `down`). Wrapped key
  material (KEKs, DEKs) is restored, but nothing can unseal it without the key.
- Before you begin a restore, **confirm you can retrieve the `MASTER_KEY` that
  was in effect when the backup was taken.** If the key was rotated after the
  backup, you also need the previous key (`MASTER_KEY_PREVIOUS`) so rotation can
  re-wrap material.

Treat losing `MASTER_KEY` as losing all encrypted content. Back the key up
separately, under its own custody, with the same care as this database.

## Prerequisite: a `pg_dump` at least as new as the server

`pg_dump` refuses to dump a server newer than itself and aborts, so the client
major in `Dockerfile.coolify` must be greater than or equal to the `postgres`
image major in `compose.coolify.yaml`. They are pinned to 17 at both sites and
must be bumped together.

Debian bookworm ships only `postgresql-client` 15, so the image installs
`postgresql-client-17` from PGDG. Getting this wrong fails every backup with
`MetadataDumpError` and nothing else -- stderr is deliberately discarded to keep
the DSN out of the logs. The `metadata_tool_failed` log line carries the client
version for exactly this case; compare it against the server.

## Selecting a backup

List available backups from the admin operations surface:

```
GET /api/v1/admin/operations/backups
```

Each entry is identified by timestamp, verification state, size, and schema
revision. **Only restore a `verified` backup** — a `failed` one is a partial
artifact that never passed integrity verification. The operations summary
(`GET /api/v1/admin/operations`) also shows the last backup's time, outcome,
duration, size, verification state, and a staleness alert.

## Running a restore

The restore procedure is scripted:

```
just restore <backup-id>                 # non-destructive (default)
just restore <backup-id> --destructive   # overwrite a non-empty target
```

or directly:

```
python -m cyberfs.restore --backup-id <uuid> [--destructive]
```

The command reads its target from the environment (`DATABASE_URL`,
`MINIO_*`, `BACKUP_S3_*`, `MASTER_KEY`). It:

1. Refuses a target that already holds data unless `--destructive` is given
   (non-destructive by default — see below).
2. Loads the Postgres dump into the target database via `pg_restore`.
3. Applies any migrations between the backup's recorded schema revision and the
   deployed head, and **reports the upgrade path taken** (migration skew is
   handled automatically).
4. Mirrors every object from the backup target into the primary content bucket,
   ciphertext untouched.
5. Reports success **only if** the restored stack passes its readiness probe
   (database reachable, object store reachable) **and** `MASTER_KEY` opens the
   restored key material.

On success it prints the migration upgrade path and a health summary, and exits
`0`. If the key is unavailable the objects are still restored but the command
exits non-zero and reports `healthy: False`.

### Requirements

- `pg_dump` / `pg_restore` (`postgresql-client`) must be installed on the host
  running the restore. Their absence surfaces as a clear
  `backup_tool_unavailable` error.
- Network access to the target Postgres and both MinIO endpoints.

### The dump's name understates what it is

The artifact is stored as `dump.sql.gz`, and it is **neither SQL nor gzip**. It is
a `pg_dump --format=custom` archive — verify with its first five bytes, which are
`PGDMP`. The name is historical and worth knowing about before an incident,
because the obvious manual reading of it is wrong:

```sh
gunzip -c dump.sql.gz | psql ...   # WRONG: not gzip, not SQL; fails immediately
pg_restore --no-owner --no-privileges -d "$TARGET" dump.sql.gz   # correct
```

`just restore` uses `pg_restore` already, so the automated path is unaffected;
this matters only for a manual restore, which is exactly the situation where the
extension is the first thing anyone looks at. A custom-format archive is also
already compressed, so it does not need decompressing.

### What a restored dump is a snapshot *of*

The manifest records `schema_revision` — the Alembic head at backup time. A dump
restored into an empty database therefore arrives at *that* revision, not
necessarily at the code's current head, so migrations still have to run afterwards
if the deployment has moved on. Check it before restoring:

```sh
python -c "import json,sys; print(json.load(sys.stdin)['schema_revision'])" < manifest.json
uv run alembic heads   # compare
```

## Non-destructive by default

A restore **refuses** to run against a stack that already contains data unless
you pass `--destructive`. This prevents an accidental overwrite of a live
deployment. An empty stack (fresh database, empty bucket) restores without the
flag.

## Restoring into a scratch stack

To restore without touching production — for a drill, a forensic copy, or to
recover a single file — point the environment at a **separate** database and a
**separate** target bucket:

```
DATABASE_URL=postgresql+asyncpg://.../cyberfs_scratch \
MINIO_ENDPOINT=scratch-minio:9000 \
MINIO_BUCKET=cyberfs-scratch \
BACKUP_S3_ENDPOINT=... BACKUP_S3_BUCKET=... \
MASTER_KEY=<the key in effect for that backup> \
python -m cyberfs.restore --backup-id <uuid>
```

The scratch database and bucket must be empty (or pass `--destructive`). Nothing
in the restore path writes to the production stack when the environment points
elsewhere.

## Verifying a restore

- `python -m cyberfs.restore` exits `0` and prints `healthy: True`.
- `GET /health/ready` on the restored stack returns `ready`.
- Spot-check a known file: download it and compare bytes; for an encrypted file,
  confirm it decrypts for its owner and for a share recipient.

The backup/restore round trip is exercised automatically in CI
(`tests/integration/test_backup_restore_roundtrip.py`), which seeds encrypted and
unencrypted files, multiple versions, shares, and trashed nodes, backs them up,
restores into a scratch stack, and asserts byte-level fidelity.
