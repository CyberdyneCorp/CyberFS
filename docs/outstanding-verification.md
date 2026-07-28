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

**The artifact is verified; the restore is not.** The 2026-07-28 03:00 UTC
production backup was pulled from `cyberfs-backups` and checked:

- `dump.sql.gz` + `manifest.json` + **85 mirrored objects**, matching the
  manifest's `object_count` and its 85 `entries`.
- The manifest's `dump_checksum` equals the SHA-256 of the stored bytes
  (`a285b8e3…`), so the artifact is intact rather than truncated.
- First five bytes are `PGDMP` — a real `pg_dump --format=custom` archive.
- `MASTER_KEY` is absent from **both** dump and manifest, which is the security
  property the spec requires.
- `schema_revision` is `b7e3c9a1d2f5`, one behind head, so a restore of this
  artifact needs migrations run afterwards.

**What remains is loading it into a Postgres and reading content back.** That
needs a scratch stack: production's database host is an internal Compose name and
is unreachable from a workstation, and restoring anywhere real is destructive.
Blocked on a local Docker daemon — Docker Desktop's backend runs but its Linux VM
never boots, so `docker info` fails.

Note for whoever does it: the artifact is named `dump.sql.gz` and is **neither
SQL nor gzip**. `gunzip | psql` fails; `pg_restore` is required. `just restore`
already does the right thing.

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

## 4. Migration rollback

**Spec:** none directly; every change's `design.md` claims a rollback path.

`alembic upgrade head` runs in CI and on every deploy. `downgrade` is written for
all **eight** migrations and is currently exercised for none of them, so the
rollback plan in each design document is unproven **in the present tree**.

It was proved once. The tests in item 9 walked the whole chain down to base and
back up in CI (run 30351989609, 341 integration tests) before being reverted
during the outage recovery. Re-applying them closes this item outright — the work
exists and passed, it is simply not in the tree.

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

An earlier note here claimed no in-process test could cover the distinction,
because every suite stubs the directory and a stub cannot be missing a scope. That
was wrong: driving `CyberdyneDirectory` through `httpx.MockTransport` covers it
exactly, and fifteen such tests were written — for an adapter that until then had
none. They are part of the reverted work in item 9.

## 8. `MKCOL` on an existing collection answers the wrong status — **closed**

**Spec:** `webdav-compatibility`.

Every taken-name refusal on the WebDAV surface returned `412`, where RFC 4918
§9.3.1 names `405` for a `MKCOL` on an already-mapped URL; `412` stays correct for
`COPY`/`MOVE` (§9.8.5). It matters because a sync client calls `MKCOL` on
directories that may already exist and reads `405` as "already there, carry on",
while `412` is a precondition it never set.

Fixed in `adapters/inbound/api/routers/webdav.py`, pinned at the integration and
e2e tiers, and confirmed against the deployment before the outage —
`tests/e2e/test_live_webdav.py` passed in the 82/12/0 run.

## 9. Work reverted during the outage, awaiting re-application

Recovering the 2026-07-28 outage meant reverting `dcdbcaf..8fd349c` wholesale,
because the cause was not established and the priority was restoring service. The
revert is at `f2fded4`, whose tree is byte-identical to `dd1a500`. None of the
work below was wrong — it is simply no longer in the tree, and each piece is worth
re-applying once the deployment is healthy:

- **The directory refusal-versus-outage distinction.** A `403 Insufficient scope`
  from CyberdyneAuth was reported as `dependency_unavailable`/503, which reads as
  transient and invites a retry that can never succeed. `DependencyForbiddenError`
  maps to 502 and names the missing scope. Came with **fifteen tests** for
  `CyberdyneDirectory`, an adapter that previously had none — every sharing suite
  stubs the port, which is exactly why the production break was invisible.
- **Migration rollback tests.** These *closed* item 4 in CI (run 30351989609, 341
  integration tests): the whole chain down to base and back up, a single step
  against the newest revision, and the sealing-id backfill checked on a row seeded
  at the previous revision.
- **The settings-reachability finding.** 53 of 74 settings could not be set on the
  deployment because `compose.coolify.yaml` names only 21 in its `environment:`
  block. `MASTER_KEY_PREVIOUS` was among them, which makes the documented online
  key-rotation procedure impossible to perform. **The finding stands; both
  attempted fixes failed in production** — `KEY: ${KEY:-}` defines the variable as
  an empty string, which 49 non-optional settings cannot parse, and a bare `- KEY`
  in list form resolves from the shell environment rather than the `.env` file
  Coolify writes. A third attempt must be validated against a real
  `docker compose config` run before it goes near a deployment.
- **The workload tier**, which seeded 240 nodes on the deployment and walked the
  paginated surfaces.
- **Runbook corrections** about the dump's true format and its `schema_revision`.

