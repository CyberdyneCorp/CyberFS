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

## 6. Scheduled backup and retention

**Spec:** `backup-restore` — "Scheduling and target", "Retention".

`BACKUP_CRON` defaults to `0 3 * * *` UTC and has never fired in production; every
backup so far was triggered by hand. Retention is thoroughly unit-tested but has
never pruned a real artifact — production holds two backups and the failed one
should disappear once `BACKUP_FAILED_GRACE_HOURS` passes. Both are answered by
waiting and looking rather than by any code change.
