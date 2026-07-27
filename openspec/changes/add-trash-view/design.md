## Context

Soft delete is already a subtree operation. `NodeService.delete`
(`application/nodes.py:375`) authorizes `OWNER`, measures the subtree's bytes,
and calls `NodeRepository.soft_delete_subtree`, which stamps one `now` onto the
root and every descendant that is not already trashed
(`adapters/outbound/db/repositories.py:188`) and bumps each row's revision.

Nothing reads those rows back on behalf of a user:

- `list_children` adds `deleted_at IS NULL` unless `include_deleted` is passed,
  and the API never passes it (`repositories.py:122`, `routers/nodes.py:83`).
- `search` hard-codes `deleted_at IS NULL` (`repositories.py:239`).
- `NodeService._authorize` treats a trashed node as absent, so
  `GET /nodes/{id}` returns `404` (`application/nodes.py:108`).
- `list_trashed_before` exists but is the retention sweep's driver: it is
  cutoff-scoped and crosses users, and no route reaches it.

`NodeService.restore` (`application/nodes.py:402`) then clears `deleted_at` on
exactly one row -- `node.restore(moment)`, `uow.nodes.update(node)` -- while
computing the bytes to un-trash from `descendants(include_deleted=True)`. So the
quota already behaves as though the subtree came back, and the rows do not.
Nothing pins this: no test restores a folder that has descendants
(`tests/unit/test_node_service.py:473`, `tests/integration/test_api_nodes.py:277`
both restore a single node).

## Goals / Non-Goals

**Goals:**

- The delete → find → restore loop closes without the client having remembered
  an id.
- One trash entry per delete, carrying enough to decide restore-or-purge without
  a second request.
- Restore returns what the delete removed, and the quota buckets agree with the
  rows afterwards.
- Emptying the trash in one call, with the irreversibility guarded rather than
  assumed.

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
the only rule the backfill could use to reconstruct batches is exactly the rule
being replaced. The derived rule is available today, needs no schema change, and
cannot disagree with itself. The column stays the answer if the parent check ever
shows up in a query plan.

**Restore takes the nodes that share the entry's `deleted_at`.**
`soft_delete_subtree` writes one timestamp value across the whole `UPDATE`, so
membership in a delete is recorded already; equality is exact, not approximate.
Two alternatives were considered. Restoring the *entire* trashed subtree
resurrects a file the user deliberately deleted an hour before they deleted its
folder -- the delete already refuses to re-stamp such a node
(`WHERE deleted_at IS NULL`), so overriding that on the way back would discard
the one distinction the delete bothered to preserve. Restoring only the entry is
today's behaviour and is the bug. The trade-off of the timestamp rule: two
independent deletes could in principle land on the identical instant, and to
matter they would have to be nested -- an inner node deleted in the same
microsecond as its ancestor, which then gets restored with it. Harmless, and
worth a comment at the query.

**Only the entry's name is checked for a collision on restore.** A trashed folder
cannot gain live children: creating a child requires authorizing the parent, and
`_authorize` refuses a trashed node. So no live sibling can have taken a
descendant's name while the subtree sat in the trash, and the existing
`_ensure_name_free` call at the entry remains the only one needed. Every restored
row still bumps its revision, so a client holding a descendant's pre-delete ETag
is refused rather than silently accepted.

**`GET /api/v1/trash`, a top-level collection.** The trash is per-user and spans
the whole tree, so hanging it off a folder (`/nodes/{id}/trash`) would be a lie
about what it contains. `/api/v1/search` is the precedent: a caller-scoped
cross-tree query at the top level. `/api/v1/me/*` is the activity surface, where
nothing addresses a node and nothing is mutable.

**Entry totals are computed for a whole page in one aggregate.** A folder's own
`size_bytes` is `0`, so reporting it would put "0 bytes" beside every deleted
folder -- the single number the user needs in order to choose between restore and
purge. One recursive aggregate seeded with the page's ids costs one query per
page and is bounded by `PAGE_SIZE_MAX`, where a per-entry walk would be up to a
thousand.

**Newest deletion first, cursor-paginated, and the "parent not trashed" test
lives in the `WHERE` clause.** Filtering after limiting would return short and
occasionally empty pages while more entries existed. A partial index on
`(owner_id, deleted_at)` where `deleted_at IS NOT NULL` narrows to the caller's
trash first; the parent test is then a primary-key lookup per candidate row. The
existing `ix_nodes_deleted_at` cannot serve this -- it is deliberately not
owner-scoped, because the sweep it exists for reads across all users.

**The trash listing is not cached.** Caching it would mean invalidating on every
delete, restore, and purge anywhere in the caller's tree, and a stale trash view
fails in the two worst ways available: an entry that is missing cannot be
restored, and an entry that is gone 404s when the user clicks restore. The read
is rare and already indexed.

**Emptying the trash is confirmed by the entry count.** The request states how
many entries it means to destroy, and a trash holding a different number is
refused with `409` and destroys nothing. A `confirm: true` flag would be a
constant that no client could get wrong, which is precisely why it is no
evidence that the caller looked at what they are destroying; the count can only
be right if they listed the trash, and it goes stale exactly when something
changed underneath them. This is the same discipline `If-Match` already provides
for a single node, applied to a collection.

**`POST /api/v1/trash/purge`, not `DELETE /api/v1/trash`.** The per-node purge
design rejected `DELETE ...?permanent=true` on the grounds that irreversibility
must not be a parameter of a recoverable verb. `DELETE` on a collection in this
API means the recoverable soft delete everywhere it appears, so reusing it for
permanent destruction of a whole trash would be the same mistake at a larger
radius. A distinct path is explicit at the call site and in the access log.

**Bounded per call, reporting what remains.** The purge design already flagged
that one deep subtree makes one slow request; a trash is many subtrees. So a call
purges at most `PAGE_SIZE_MAX` entries, oldest deletion first, and reports how
many entries are left, so a client can loop and each iteration re-confirms
against the current count. A background job was rejected as premature: the
asynchronous pattern exists (`ASYNC_REWRAP_THRESHOLD_NODES`) and is the answer if
the bound proves too small, but adding a job for an operation nobody has run yet
would be untested machinery guarding an untested endpoint.

**Emptying reuses `application/purge.py`.** `purge_one` and `purge_subtree` are
already shared by the sweep and the per-node endpoint specifically so the
destructive sequence exists once. A third caller re-deriving it is how a quota
leak or a stranded object gets introduced.

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

**`TRASH_EMPTIED` is deliberately absent from `ACTIVITY_ACTIONS`.**
`SECURITY_ACTIONS` is the complement (`domain/activity.py:50`), so the new action
is retained and never pruned. That is the intent, not an oversight: a bulk
irreversible destruction must stay attributable after the surrounding activity
records have aged out. Each node destroyed still emits the `NODE_PURGED` record
an individual purge would; the batch record carries the entry count and bytes so
one row explains a sudden drop in usage.

## Risks / Trade-offs

- **Bulk irreversible destruction from a mistaken or replayed call.**
  → Mitigation: it reaches only nodes already deliberately deleted; the entry
  count must match, so a replay after the first success is refused with `409`
  rather than destroying whatever has since been trashed; every node produces a
  retained security record and the batch produces one more.

- **Existing deployments have orphaned trashed descendants** from folders
  restored before this change. They will start appearing as trash entries.
  → Mitigation: this is the correct presentation of rows that already exist and
  are already charged to the user's quota -- previously they were invisible and
  unrecoverable. Documented in `docs/operations.md`, and the reconciliation job
  already reports usage from metadata, so the buckets converge.

- **A cascading restore can fail the sibling-name check after a partial
  restore**, if the transaction were split.
  → Mitigation: the subtree update is one statement inside the existing unit of
  work, so either the whole entry comes back or none of it does. No intermediate
  state where half a tree is visible.

- **The timestamp grouping mis-groups a nanosecond tie**, restoring an inner node
  that was deleted separately in the same instant as its ancestor.
  → Mitigation: the only outcome is that a node the user deleted seconds earlier
  comes back with its folder, which they can delete again. Pinned by a test so
  the rule is documented as intended rather than incidental.

- **The bound on emptying means a large trash needs several calls**, and a naive
  client that ignores `remaining` will believe it finished.
  → Mitigation: the response reports both what was destroyed and what remains,
  and the `409` on a stale count makes a blind retry loop fail loudly rather than
  quietly. `docs/api.md` documents the loop.

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
and reverting the restore behaviour; nothing already purged returns, and nothing
already restored breaks, because a cascading restore leaves the rows in the state
the old code claimed they were in.

## Open Questions

- **Should a trash entry expose its `ETag` for use as `If-Match` on restore?**
  Restore takes no precondition today. The entry carries the revision anyway, so
  adding the header later is compatible; deciding it now would be guessing at a
  client that does not exist.
- **Should `GET /api/v1/trash` report the trash's total bytes as a header or an
  envelope field?** The admin dashboard already reports trashed bytes per user,
  and `POST /trash/purge` reports what it freed, so the aggregate may be
  redundant on the listing. Left out until a client asks.
