# Outstanding verification

Things the specs require, the code implements, and **nobody has yet proved on real
infrastructure**. Each is an operational drill rather than a coding task, which is
why none of them can be closed by a test run.

This file exists because these items were previously tracked as unchecked tasks
inside `openspec/changes/`. Archiving a change moves its `tasks.md` into
`openspec/changes/archive/`, where an unchecked box is invisible in practice — so
anything still genuinely undone has to live somewhere that is read. Keep this
list short: an entry either gets done and deleted, or becomes a change of its own.

## 1. Restore from a real backup

**Spec:** `backup-restore` — "Restore procedure", "Restore is tested automatically".

**The artifact is now verified; the restore still is not.** Pulled the 2026-07-28
03:00 UTC production backup straight out of `cyberfs-backups` and checked it:

- `dump.sql.gz` + `manifest.json` + **85 mirrored objects**, matching the
  manifest's `object_count` and its 85 `entries` exactly.
- `dump_checksum` in the manifest equals the SHA-256 of the bytes actually stored
  (`a285b8e3…`), so the artifact is intact and not truncated.
- First five bytes are `PGDMP`, so it is a real `pg_dump --format=custom` archive
  rather than an empty or partial file.
- `MASTER_KEY` appears in **neither** the dump nor the manifest, which is the
  security property `backup-restore` requires.
- `schema_revision` is `b7e3c9a1d2f5` — one revision behind current head, so a
  restore of this artifact needs migrations run afterwards.

So the backup is fetchable, complete, internally consistent, and key-free.

**What remains is loading it into a Postgres and reading content back**, which is
the only thing that proves a restore. It needs a scratch stack: the production
database host is an internal Compose name and is not reachable from a workstation,
and restoring anywhere real is destructive. Blocked on a local Docker daemon —
Docker Desktop's backend runs but its Linux VM never boots, so `docker info` fails
and no scratch Postgres can be started.

When Docker is available:

```sh
just up                       # scratch Postgres, Redis, MinIO
just restore <backup_id>      # non-destructive against an empty stack
```

Was `bootstrap-cyberfs` task 12.8.

## 2. Multi-replica drill

**Spec:** `deployment` — the API is stateless and scales to N replicas.

Scale `api` to 2+, confirm every replica reaches ready, and confirm a file
uploaded through one downloads through another. The boot migration is supposed to
serialize under a Postgres advisory lock; that has only been reasoned about, not
observed with concurrent replicas.

Also `bootstrap-cyberfs` task 12.8.

## 3. `MASTER_KEY` custody

**Spec:** `content-encryption`, and `restore-runbook.md`'s prerequisite section.

Not a drill and not a task anyone wrote down — which is why it is here. The
production `MASTER_KEY` exists only in Coolify's environment. It is deliberately
excluded from every backup artifact, so losing Coolify's configuration loses all
encrypted content permanently, and no restore can recover it.

It needs to be held somewhere else, under its own custody, before the backups
above mean anything.

## 4. Migration rollback — **closed**

**Spec:** none directly; every change's `design.md` claims a rollback path.

Proved in CI on 2026-07-28. `tests/integration/test_migrations.py` now walks the
whole chain down to base and back up on a database of its own, checks that a
single step back and forward actually removes and restores the newest column
rather than passing on a no-op `downgrade`, and verifies the sealing-id backfill
against a row seeded at the previous revision.

Driven through `command.upgrade(alembic_config(), ...)` with `DATABASE_URL`
pointed at the scratch database — the same call the container entrypoint makes —
so the rollback is exercised on the path the deployment actually uses rather than
through a synchronous driver the project does not install.

The rollback plans in each design document are therefore no longer claims. Note
what this does and does not cover: it proves the schema round-trips, not that any
particular deployment's *data* survives a downgrade. `c8f4a2e6d1b7`'s downgrade
drops a column, and its docstring says plainly that doing so returns the database
to a state where copies of encrypted content are unreadable.

## 5. Browser sign-in

**Spec:** `admin-dashboard` — "Dashboard access and session behaviour".

Both paths are verified up to the point a browser takes over: the Google
authorization URL is returned correctly, and password sign-in returns a token
pair whose profile reports `is_admin`. Neither consent round trip has been driven
in a real browser.

## 6. Backup retention

**Spec:** `backup-restore` — "Retention".

**Scheduling is now proved.** `BACKUP_CRON` (`0 3 * * *` UTC) fired in production on
2026-07-27 at `03:00:00.027634Z` and the artifact verified: 25.85 s, 3.49 MB, 85
objects, `last_outcome: verified`, `stale: false`. The sub-second offset from
exactly 03:00 is the scheduler, not a hand trigger. Read it back any time with
`GET /api/v1/admin/operations`, which `tests/e2e/test_live_admin.py` now covers.

**Retention still has not pruned anything.** The register holds three artifacts,
including the failed run from 2026-07-26 13:32 — and `BACKUP_FAILED_GRACE_HOURS`
defaults to 24, which elapsed over a day ago. So either the retention sweep is not
running or it is not removing failed artifacts, and the difference matters: the
first is a scheduling problem, the second is a bug in the sweep. Establish which
before assuming this closes itself by waiting.

## 7. A directory authorization failure is reported as an outage

**Spec:** `sharing` — recipient resolution.

**The production symptom is fixed.** `PUT /api/v1/nodes/{id}/grants` with an email
recipient answered `503 "the user directory is unavailable"` for *every* address,
because CyberFS's OAuth client (`cyb_IISsKa9xrdPaFzIJ`) was registered in
CyberdyneAuth with `scope: ""` and `GET /orgs/{id}/members` refused it with
`403 Insufficient scope: directory:read required`. Granting that one scope fixed
it with no deploy; `tests/e2e/test_live_sharing.py` covers it and passes.

**What remains is the misreport.** `adapters/outbound/auth/directory.py` maps any
`httpx.HTTPError` from that call to `DependencyUnavailableError`, so an
authorization failure presents as a transient outage. A `403` there means "CyberFS
is not allowed to ask", which is permanent and actionable, while `503` invites a
retry that can never succeed — and it cost real time diagnosing exactly this.
Worth separating the two before the next misconfiguration hides behind the same
message.

Note also that no in-process test can cover the distinction: the integration suite
stubs the directory, and a stub cannot be missing a scope. It belongs to the e2e
tier or nowhere.

## 8. `MKCOL` on an existing collection answers the wrong status — **closed**

**Spec:** `webdav-compatibility`.

Every taken-name refusal on the WebDAV surface returned `412`, where RFC 4918
§9.3.1 names `405` for a `MKCOL` on an already-mapped URL; `412` stays correct for
`COPY`/`MOVE` (§9.8.5). It matters because a sync client calls `MKCOL` on
directories that may already exist and reads `405` as "already there, carry on",
while `412` is a precondition it never set.

Fixed, deployed, and confirmed against the deployment by
`tests/e2e/test_live_webdav.py`, which now passes.
