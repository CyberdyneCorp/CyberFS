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

Done: backups are enabled against a separate MinIO instance over TLS, and a
seeded run produced a `verified` artifact — dump, manifest, and 87 mirrored
objects off-site, `skew_missing_in_manifest` 0, `MASTER_KEY` confirmed absent
from both the real manifest and the real dump, plain and encrypted seed files
reading back byte-identical.

Not done: **restoring from it.** The automated round trip in
`tests/integration/test_backup_restore_roundtrip.py` runs in CI, so the code path
is exercised — but with CI-made artifacts, on CI's Postgres, under CI's key. It
does not prove that *this* dump, from *this* server, with *this* `MASTER_KEY`,
restores. Restoring is destructive, so it needs a scratch stack; see
[`restore-runbook.md`](restore-runbook.md).

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
all seven migrations and **exercised for none of them**. The rollback plan in each
design document is therefore unproven.

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

## 7. Sharing by email is broken in production

**Spec:** `sharing` — recipient resolution.

`PUT /api/v1/nodes/{id}/grants` with an email recipient answers `503 "the user
directory is unavailable"` for *every* address, including ones that certainly
exist. Sharing by subject UUID works, so only the email path is affected.

The cause is proved and is configuration, not code: CyberFS's OAuth client
(`cyb_IISsKa9xrdPaFzIJ`) is registered in CyberdyneAuth with `scope: ""`, so
`GET /orgs/{id}/members` refuses it with
`403 Insufficient scope: directory:read required`. Granting that one scope to the
client fixes it; nothing needs deploying.

Two things follow that *are* code:

- `adapters/outbound/auth/directory.py` maps any `httpx.HTTPError` from that call
  to `DependencyUnavailableError`, so an authorization failure is reported as an
  outage. A `403` from the directory means "CyberFS is not allowed to ask", which
  is permanent and actionable; `503` invites a retry that can never succeed.
- No test could have caught it. The integration suite stubs the directory, and a
  stub cannot be missing a scope. `tests/e2e/test_live_sharing.py` now asserts the
  working behaviour and fails until the scope is granted.

## 8. Copying or restoring a version of an encrypted file yields an empty body

**Spec:** `content-encryption` — framing; `file-storage` — versions, copy.

**This one loses data from the user's point of view and needs a decision.**

`EncryptionService.seal` binds every frame to the version id
(`cipher.seal(plaintext, dek, version_id.bytes)`), and `open` authenticates with
`version.id` from the row it is serving. Two paths copy *sealed bytes* into a row
that carries a **different** id:

- `ContentService.restore_version` — copies the source object to a new key and
  writes a new `FileVersion` with a fresh id.
- `ContentService._copy_content` (behind `POST /nodes/{id}/copy`) — the same
  shape, for a new node.

In both, `open` then authenticates against an id the bytes were never sealed
under, decryption produces nothing, and the endpoint returns `200` with an empty
body. Restore additionally repoints `current_version_id`, so a user who rolls
back an encrypted file watches it silently empty itself while every byte is still
in the object store, unreadable.

Pre-existing, from the same commit as the encryption feature. It survived because
no test copied or restored a version of an *encrypted* file — the existing copy
and restore tests use plaintext, and the encryption tests never copy.

Pinned by `tests/integration/test_api_content.py::
test_restoring_a_version_of_an_encrypted_file_returns_its_bytes`, marked
`xfail(strict=True)` so that fixing the bug fails the run until the marker is
removed.

Two candidate fixes, and they differ in cost rather than correctness:

1. **Re-seal on copy** — open the source stream with the source id and seal it
   again under the new one. Self-contained, no migration, but it decrypts and
   re-encrypts the whole object on every copy and version restore.
2. **Record the sealing id on the version row** — add a column that defaults to
   the row's own id, set it to the source's when copying, and have `open` use it.
   One migration, no re-encryption, and it makes the AAD explicit rather than
   implied by a column that happens to share a name.

## 9. `MKCOL` on an existing collection answers the wrong status

**Spec:** `webdav-compatibility`.

Fixed in `adapters/inbound/api/routers/webdav.py` but **not yet deployed**: every
taken-name refusal on the WebDAV surface returned `412`, where RFC 4918 §9.3.1
names `405` for a `MKCOL` on an already-mapped URL. `412` stays correct for
`COPY`/`MOVE` (§9.8.5). It matters because a sync client calls `MKCOL` on
directories that may already exist and reads `405` as "already there, carry on",
while `412` is a precondition it never set. Found by running
`tests/e2e/test_live_webdav.py` against the deployment; that test fails until the
fix ships.
