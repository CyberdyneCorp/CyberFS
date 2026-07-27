## Context

Soft delete is already a subtree operation, and so is restore. `NodeService.delete`
(`application/nodes.py:375`) authorizes `OWNER`, calls
`NodeRepository.soft_delete_subtree` -- which stamps one `now` onto the root and
every descendant not already trashed, bumps each row's revision, and returns the
rows it moved -- and charges only those rows' bytes to the trashed bucket.
`NodeService.restore` (`application/nodes.py:406`) calls
`NodeRepository.restore_subtree`, which clears `deleted_at` on the entry and on
every descendant whose `deleted_at` equals the entry's
(`adapters/outbound/db/repositories.py:214`), and returns to the live bucket only
the bytes of the rows it cleared (`nodes.py:449`). `_subtree_bytes` no longer
exists. The unit suite pins all of it: `test_restore_brings_back_the_deleted_subtree`,
`test_restore_returns_the_subtree_bytes_to_the_live_bucket`,
`test_a_child_deleted_before_its_parent_stays_trashed`,
`test_restore_advances_the_revision_of_every_row_it_lifts`.

**This change inherits that and adds nothing to it.** The rules stated below about
delete batches and timestamp equality are recorded because the listing's collapse
rule depends on them, not because they are being introduced here.

What is still missing is any way to reach a trashed row:

- `list_children` adds `deleted_at IS NULL` unless `include_deleted` is passed,
  and the API never passes it (`repositories.py:122`, `routers/nodes.py:83`).
- `search` hard-codes `deleted_at IS NULL` (`repositories.py:239`).
- `NodeService._authorize` treats a trashed node as absent, so
  `GET /nodes/{id}` returns `404` (`application/nodes.py:108`).
- `list_trashed_before` exists but is the retention sweep's driver: it is
  cutoff-scoped and crosses users, and no route reaches it.

And one thing that only matters once a trashed row *is* reachable: `restore`
calls `_ensure_name_free` at the destination (`nodes.py:434`), while
"Name validity and uniqueness" → "Name reusable after deletion"
(`openspec/specs/file-storage/spec.md:39`) lets a live sibling take the trashed
node's name. Today no caller can find the id, so the refusal is unreachable. A
trash listing makes it a routine user action, and nothing specifies it.

## Goals / Non-Goals

**Goals:**

- The delete → find → restore loop closes without the client having remembered
  an id.
- One trash entry per delete, carrying enough to decide restore-or-purge without
  a second request.
- Emptying the trash in one call, with the irreversibility guarded rather than
  assumed, and with the work one call performs actually bounded.
- The one way the loop can fail -- a name taken while the node sat in the trash
  -- is specified, refused cleanly, and tested.

**Non-Goals:**

- **An administrative trash listing.** `admin-dashboard/spec.md` withholds node
  names from administrators unless `ADMIN_SHOW_FILENAMES` is set, and enabling it
  is audited. A trash listing on the admin surface would hand over names, paths
  and sizes with no gate at all. Administrators keep
  `POST /nodes/{id}/purge` for a node named in an audit record, which is the
  compliance path that actually exists.
- **Browsing inside a trashed folder.** The entry reports its total bytes and node
  count; a listing of a trashed subtree would need every read path to start
  distinguishing "the owner inspecting their own trash" from "a probe", and the
  question it answers is "how big is this", which the entry answers.
- **Reading a trashed node through `GET /nodes/{id}`.** Same reason. The trash
  entry is self-contained precisely so this stays closed.
- **Changing restore's cascade, its quota arithmetic, or its single
  `NODE_RESTORED` record.** All three are already correct on `main`.
- **Restore under a new name, or a `force` that displaces the occupant.** Argued
  below.
- **Purging individual versions.** Argued below.
- **Undoing a purge, or any statement about backups.** Purge is irreversible;
  `docs/restore-runbook.md` governs what a prior backup holds.
- **Changing `TRASH_RETENTION_DAYS`, the sweep, or per-node purge.**
- **Bulk restore.** Restore stays one entry per call. Nothing about a listing
  makes multi-restore urgent, and unlike purge there is no irreversibility to
  batch away.

## Decisions

**A trash entry is a trashed node whose parent is absent or not trashed.** The
alternative -- listing every row with `deleted_at` set -- is not merely noisy.
Two things break with it. A descendant's path cannot be rendered as anything a
user recognizes, because its ancestors are trashed too and are not in the live
tree. And `restore` on a descendant hits the "parent is deleted" branch and
reparents it into the owner's root folder: offering 400 restore buttons, 399 of
which silently flatten the user's hierarchy into their root, is worse than
offering none. Collapsing to the node whose parent survived gives every entry a
real path, and makes the one restore offered the one that reconstructs the tree.

**Derived from the parent link, not stored in a new column.** A
`trashed_root_id` (or delete-batch id) written by `soft_delete_subtree` would be
more explicit and would make the listing a plain indexed scan instead of a
parent lookup per candidate row. It was rejected for now because it needs a
migration *and* a backfill over trash that already exists in the deployment, and
the only rule the backfill could use to reconstruct batches is the parent rule
itself. The derived rule is available today, needs no schema change, and cannot
disagree with itself. The column stays the answer if the parent check ever shows
up in a query plan.

**The collapse rule leans on the timestamp rule already in the repository.**
`soft_delete_subtree` writes one `now` across the whole `UPDATE`, so membership in
a delete is recorded, and `restore_subtree` lifts exactly that batch. This is why
an inner node trashed on an earlier occasion survives its parent's restore and
must then appear as an entry of its own -- the listing has to present what the
restore actually did. Nothing here changes either statement; the comment at the
equality predicate (`repositories.py:218`) is already in place.

**Only the entry's name is checked for a collision on restore.** A trashed folder
cannot gain live children: creating a child requires authorizing the parent, and
`_authorize` refuses a trashed node. So no live sibling can have taken a
*descendant's* name while the subtree sat in the trash, and the existing
`_ensure_name_free` call at the entry remains the only one needed.

**A restore whose name is taken is refused, and the entry does not advertise
restorability.** `409 name_taken`, nothing restored -- the check runs before
`restore_subtree`, so the refusal cannot leave half a tree lifted. Three
alternatives were rejected. *A `restorable` flag on each entry* would cost a
sibling lookup per entry on a read that is otherwise one scan plus one aggregate,
and would still be a guess by the time the user clicked -- the flag can go stale
between the listing and the restore, so the refusal has to exist and be handled
regardless. *Restore under a caller-supplied name* makes restore a rename too, and
the name it would have to rename is the one the user is trying to recover;
`POST /nodes/{id}/restore` then needs a body, a validation path, and a story for
what the descendants are called. *Displacing the live occupant* trades a refusal
for silent data movement. The honest resolution is the one the user already has
words for: rename the occupant, then restore. `docs/api.md` says so, and the
`409` carries the code that tells a client which of the two things went wrong.

**`GET /api/v1/trash`, a top-level collection.** The trash is per-user and spans
the whole tree, so hanging it off a folder (`/nodes/{id}/trash`) would be a lie
about what it contains. `/api/v1/search` is the precedent: a caller-scoped
cross-tree query at the top level. `/api/v1/me/*` is the activity surface, where
nothing addresses a node and nothing is mutable.

**The listing reports the total number of entries, not just a page of them.** The
purge guard below requires the number the trash holds, and with
`PAGE_SIZE_MAX` at 1000 a first-time caller would otherwise have to paginate the
entire trash to obtain the one value that makes the guard passable -- a guard
nobody can satisfy on the first call is not a guard, it is a wall. So the listing
envelope carries `total_entries` beside `items` and `next_cursor`: one `COUNT`
over the same predicate and the same partial index as the page. It is the only
listing in the API that reports a total, and it earns the exception by being the
input to a destructive precondition rather than a UI nicety. Total *bytes* stays
out -- the admin dashboard already reports trashed bytes per user, `POST
/trash/purge` reports what it freed, and no guard needs it.

**Entry totals are computed for a whole page in one aggregate.** A folder's own
`size_bytes` is `0`, so reporting it would put "0 bytes" beside every deleted
folder -- the single number the user needs in order to choose between restore and
purge. One recursive aggregate seeded with the page's ids costs one query per
page and is bounded by `PAGE_SIZE_MAX`, where a per-entry walk would be up to a
thousand. `empty_trash` reuses the same aggregate, which is what lets it spend a
node budget without starting an entry blindly.

**Newest deletion first, cursor-paginated, and the "parent not trashed" test
lives in the `WHERE` clause.** Filtering after limiting would return short and
occasionally empty pages while more entries existed. A partial index on
`(owner_id, deleted_at)` where `deleted_at IS NOT NULL` narrows to the caller's
trash first; the parent test is then a primary-key lookup per candidate row. The
existing `ix_nodes_deleted_at` cannot serve this -- it is deliberately not
owner-scoped, because the sweep it exists for reads across all users.

**A trash entry is not a node-metadata read.** "The content digest is reported"
and "Effective permission reported" (`file-storage/spec.md:326`, `:311`) are
phrased over a caller reading a node's metadata; a trash entry carries neither
digest nor effective permission and does not contradict them. An entry is a
restore-or-purge handle, not a handle on content: the digest is withheld because
nothing can be downloaded through this surface, and the permission is omitted
because the listing is owner-scoped by construction -- every entry in it is the
caller's own, so a field repeating `owner` on every row would be noise. Stated
here so a later reader diffing the schemas against those scenarios does not read
a conflict into them.

**The trash listing is not cached.** Caching it would mean invalidating on every
delete, restore, and purge anywhere in the caller's tree, and a stale trash view
fails in the two worst ways available: an entry that is missing cannot be
restored, and an entry that is gone 404s when the user clicks restore. The read
is rare and already indexed.

**A subtree restore invalidates the rows it lifted, not only the entry.** The
inherited `restore` calls `_invalidate(node.id, old_parent=..., reparented=True)`,
which drops the entry's node key, its parent's listing prefix, and every
permission decision -- but not the node keys of the descendants it just mutated,
nor the entry's own children-listing prefix. `caching/spec.md`, "Invalidation on
mutation", requires a subtree mutation to drop cached decisions and listings for
descendants, and restore became a subtree mutation. The practical risk today is
nil, because nothing populates `Dataset.METADATA` or `Dataset.LISTING` on read
and the invalidation is write-only -- which is exactly why it must be fixed while
it is free rather than when a read path starts trusting it. `restore_subtree`
already returns the rows it cleared, so the fix iterates a list we hold.

**Emptying the trash is confirmed by the entry count.** The request states how
many entries the trash holds, and a trash holding a different number is refused
with `409 trash_count_mismatch` and destroys nothing. A `confirm: true` flag would
be a constant that no client could get wrong, which is precisely why it is no
evidence that the caller looked at what they are destroying; the count can only
be right if they listed the trash, and it goes stale exactly when something
changed underneath them. This is the same discipline `If-Match` already provides
for a single node, applied to a collection. It follows that the stated number is
the trash's current total, not a private intention: after a bounded call destroys
some of it, the client restates the smaller total -- which is the number the
response just reported as remaining. A dedicated error code, rather than a bare
`ConflictError`, so a client can distinguish "your count is stale, list again"
from every other `409` this API returns.

**`POST /api/v1/trash/purge`, not `DELETE /api/v1/trash`.** The per-node purge
design rejected `DELETE ...?permanent=true` on the grounds that irreversibility
must not be a parameter of a recoverable verb. `DELETE` on a collection in this
API means the recoverable soft delete everywhere it appears, so reusing it for
permanent destruction of a whole trash would be the same mistake at a larger
radius. A distinct path is explicit at the call site and in the access log.

**Bounded by nodes destroyed, in whole entries, at least one.** An entry is a
subtree root, so a bound of `PAGE_SIZE_MAX` *entries* is not a bound at all: a
thousand entries of ten thousand nodes each is ten million rows stripped and
deleted in one transaction, and `purge_subtree` loops over every node in a
subtree twice with no ceiling of its own. So the bound is `TRASH_PURGE_NODE_BUDGET`
nodes (a limit, hence a constant in `domain/nodes.py` beside the tag and metadata
limits, not a setting). Entries are taken oldest deletion first; the page-wide
aggregate already reports each entry's node count, so a call stops *before*
starting an entry that would overrun the budget, and never leaves one half
destroyed -- a partially stripped entry would still list, with a subtree that no
longer matches its reported totals, which is a worse state than an entry not yet
touched. One exception, or the loop never terminates: if nothing has been
destroyed yet, the oldest entry is destroyed however large it is. That leaves the
known cost of a single deep subtree exactly where `POST /nodes/{id}/purge`
already has it -- unchanged, not multiplied by a page. A background job was
rejected as premature: the asynchronous pattern exists
(`ASYNC_REWRAP_THRESHOLD_NODES`) and is the answer if one entry proves too slow,
but adding a job for an operation nobody has run yet would be untested machinery
guarding an untested endpoint.

**Emptying reuses `application/purge.py`.** `purge_one` and `purge_subtree` are
already shared by the sweep and the per-node endpoint specifically so the
destructive sequence exists once. A third caller re-deriving it is how a quota
leak or a stranded object gets introduced.

**`empty_trash` emits its own `NODE_PURGED` records, one per entry.** Neither
`purge_one` nor `purge_subtree` emits any audit record; the only `NODE_PURGED`
emitter in the codebase is `NodeService.purge` (`nodes.py:505`), which writes one
record naming the entry root with `nodes`, `objects`, and `bytes` counts in its
context, and `PurgeJob` writes none. So there is nothing for `empty_trash` to
inherit, and the granularity is a decision, not an observation: **one record per
entry, identical in shape to what an individual purge of that entry produces**,
plus one `TRASH_EMPTIED` for the batch. Per-node records were rejected -- they
would make emptying a trash the only purge path in the system whose audit trail
is shaped differently from every other purge, and would flood the security
records that must never be pruned with rows naming descendants no user ever
addressed. The emission moves into one small helper that `purge` and
`empty_trash` both call, so the two cannot drift; it stays in the application
layer rather than moving into `purge.py`, because `purge.py`'s other caller is
the sweep, which has no actor to attribute and must keep emitting nothing.

**`TRASH_EMPTIED` is deliberately absent from `ACTIVITY_ACTIONS`.**
`SECURITY_ACTIONS` is the complement (`domain/activity.py:50`), so the new action
is retained and never pruned. That is the intent, not an oversight: a bulk
irreversible destruction must stay attributable after the surrounding activity
records have aged out. The batch record carries the entry count and reclaimed
bytes, so one row explains a sudden drop in usage; the per-entry `NODE_PURGED`
records say what it was.

**Whole-trash emptying is not exercised against a live deployment.** The e2e tier
runs against real data in a real account (`justfile`'s warning on `test-e2e`), and
`tests/e2e/conftest.py` is careful about it: one identifiable scratch folder,
trashed then purged by id on the way out. Emptying that account's trash would
destroy whatever else is in it, including data no test created and no test can
put back, and it would make teardown depend both on the feature under test and on
a count guard that any concurrent trash turns into a `409` -- failing the whole
session's teardown. So e2e covers the listing and the restore loop, cleans up by
purging its own ids, and `POST /trash/purge` is proven in the integration tier,
where the database is disposable.

**Per-version purge is not needed, and would not do the job it is asked for.**
`VERSION_RETENTION_COUNT` already bounds retained versions, so the unbounded
growth a purge would relieve cannot occur; the quota cost is bounded by
construction and already reported separately as version bytes. More importantly,
the request behind "let me purge that version" is almost always "destroy content
I should not have uploaded" -- and per-version purge does not deliver that,
because the *current* version is the one holding it and is not purgeable without
destroying the file. The honest answers are the two that already exist (upload a
replacement, or trash the node and purge it), and if selective destruction is
genuinely wanted it should be designed as redaction, on a path where the digest,
the version sequence, and the wrapped data key are considered together.

## Risks / Trade-offs

- **Bulk irreversible destruction from a mistaken or replayed call.**
  → Mitigation: it reaches only nodes already deliberately deleted; the entry
  count must match the trash's current total, so a replay after the first success
  is refused with `409` rather than destroying whatever has since been trashed;
  every entry produces a retained `NODE_PURGED` security record and the batch
  produces one more.

- **One entry can still be arbitrarily large**, and the budget's
  destroy-at-least-one rule admits it deliberately.
  → Mitigation: this is `POST /nodes/{id}/purge`'s existing cost for the same
  subtree, not a new one, and the budget stops it being paid a page at a time.
  If a single entry proves too slow the answer is the async pattern that already
  exists, not a partial purge that leaves an entry disagreeing with its own
  reported totals.

- **A restore the user expects to work can be refused** because a live sibling
  took the name.
  → Mitigation: specified, given its own error code, refused before anything is
  lifted, and documented with the resolution (rename the occupant, or purge the
  entry). The entry deliberately does not predict it, because a prediction made
  at listing time would be stale by the time it was acted on.

- **Existing deployments have orphaned trashed descendants** from folders
  restored before the cascading restore landed. They will start appearing as
  trash entries.
  → Mitigation: this is the correct presentation of rows that already exist and
  are already charged to the user's quota -- previously they were invisible and
  unrecoverable. Documented in `docs/operations.md`, and the reconciliation job
  already reports usage from metadata, so the buckets converge.

- **`total_entries` is a second query on every listing request.**
  → Mitigation: same predicate, same partial index, and the read is rare. It
  exists because the destructive guard has no other source for the number; if it
  ever shows up in a plan, the `trashed_root_id` column rejected above makes both
  queries plain index scans.

- **The bound on emptying means a large trash needs several calls**, and a naive
  client that ignores `remaining` will believe it finished.
  → Mitigation: the response reports both what was destroyed and what remains,
  and the `409 trash_count_mismatch` on a stale count makes a blind retry loop
  fail loudly rather than quietly. `docs/api.md` documents the loop.

- **Trash entry totals are a recursive aggregate over up to a page of
  subtrees**, so a user with a thousand large trashed trees pays for it on a read
  that is not authorization-critical.
  → Mitigation: one query per page, bounded by `PAGE_SIZE_MAX`, on the same
  indexed parent link the delete already walked. Worth measuring before assuming
  it needs a cached counter.

## Migration Plan

Additive. One partial index on `(owner_id, deleted_at)` where
`deleted_at IS NOT NULL`; no new column and no data change. `TRASH_EMPTIED` is a
new value in an existing audit-action column. Rollback is dropping the two routes
and the index; restore's behaviour is not part of this change and does not roll
back with it, and nothing already purged returns.

## Review dispositions

Findings from the adversarial review that were **not** acted on as written:

- *"`NodeService.restore` sums the whole trashed subtree with `_subtree_bytes`,
  so restoring a folder moves a separately deleted child's bytes out of the
  trashed bucket while its row stays trashed."* **Rejected: no longer true of the
  code.** `restore` calls `restore_subtree` and sums only the rows it returns
  (`nodes.py:439-450`), `soft_delete_subtree` returns the rows it moved, and
  `_subtree_bytes` has been deleted. Re-proposing the fix would have had this
  change rewrite working code and re-add scenarios the living spec already
  carries. The Context section above records what is inherited instead.

- *"Task 6.11 should assert the partial index is used by the listing
  (`EXPLAIN`)."* **Accepted, split rather than kept.** A plan assertion on a
  freshly migrated database with a handful of rows asserts that the planner
  prefers a sequential scan, which is correct behaviour and a flaky test. CI
  asserts the index exists (`pg_indexes`); the plan is inspected in the seeded
  measurement task, where a seeded tree makes the answer meaningful.
