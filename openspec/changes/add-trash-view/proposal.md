## Why

`file-storage/spec.md` has required a trash view since the bootstrap: a
soft-deleted node is excluded from listings and search "while retaining it in
the owner's trash view". **There is no such view.** No endpoint anywhere returns
a trashed node. `list_children` filters `deleted_at`, `search` filters
`deleted_at`, `GET /nodes/{id}` 404s a trashed node, and
`POST /api/v1/nodes/{node_id}/restore` takes an id the caller must already be
holding. So the sequence a user actually performs -- delete a file, change their
mind an hour later, go looking for it -- has no ending. The bytes are still
charged to their quota for `TRASH_RETENTION_DAYS`, the row is still there, the
restore use case works, and there is no way to learn the id. Then the purge job
destroys it. The spec says recoverable; the API says otherwise.

The cascading restore this proposal was originally written to fix has since
landed on `main`: `NodeRepository.restore_subtree` lifts the entry plus every
descendant sharing its `deleted_at`, `NodeService.restore` returns to the live
bucket only the bytes of the rows it actually cleared, `soft_delete_subtree`
returns the rows it moved so the delete charges only those, and `_subtree_bytes`
is gone. This change **inherits** all of it and re-proposes none of it; the
scenarios pinning it are already in `openspec/specs/file-storage/spec.md`. What
remains missing is the view itself -- and one consequence of exposing it: a
restore that can be refused. "Name reusable after deletion" lets a live sibling
take a trashed node's name, and `restore` still calls `_ensure_name_free`
(`application/nodes.py:434`). Unreachable while no caller could find the id; a
routine outcome once the trash is listable, and unspecified today.

## What Changes

- **`GET /api/v1/trash`** lists the caller's trashed nodes, newest deletion
  first, paginated like every other listing, and reports how many entries the
  trash holds in total -- the one number the purge guard below requires.
- **One entry per delete, not one per row.** A trash entry is a trashed node
  whose parent is not itself trashed. Deleting a 400-node folder produces one
  entry, not 400. Each entry reports its original path, when it was deleted, when
  the retention sweep will destroy it, and the total bytes and node count it
  would restore -- so the entry is self-contained and the client never has to
  read a trashed node individually.
- **A restore that cannot reoccupy its name is refused, not half-applied.**
  Restoring an entry whose name a live sibling has taken since the delete
  answers `409 name_taken` and restores nothing. The owner renames the occupant
  (or purges the entry) and retries.
- **`POST /api/v1/trash/purge`** empties the trash, guarded: the caller states
  how many entries the trash holds -- the count the listing reports -- and the
  call is refused if it holds a different number, so a bulk irreversible
  operation cannot be issued by a client that has not looked. Bounded by nodes
  destroyed rather than entries, so one call cannot strip an unbounded tree, and
  reporting what remains.
- The trash is the owner's and nobody else's. A share recipient sees nothing --
  grants stop conferring access at delete. No administrator surface is added.

Not changing: `DELETE /api/v1/nodes/{node_id}` stays a soft delete;
`POST /api/v1/nodes/{node_id}/purge` keeps its shape, its must-be-trashed `409`,
and its owner-or-admin rule; `PurgeJob` and the 30-day sweep are untouched;
trashed nodes remain absent from folder listings, search, and the S3 surface;
restore keeps its cascading semantics and its one `NODE_RESTORED` record.

## Capabilities

### New Capabilities

None. This implements a `file-storage` requirement that already exists.

### Modified Capabilities

- `file-storage`: "Soft delete, restore, and purge" gains the name-collision
  refusal on restore and the root fallback for an entry restored beneath a
  still-trashed parent; its cascading-restore and quota scenarios are reproduced
  unchanged from the living spec, not restated as new. Two requirements are added
  -- "Trash listing" and "Emptying the trash" -- rather than folding a
  caller-facing listing into "Listing, search, and metadata", whose whole subject
  is the *live* tree.

## Impact

**Affected code:**

- `src/cyberfs/domain/ports/repositories.py` -- `NodeRepository` gains a
  trash-entry listing, a total-entry count, and an aggregate for the bytes and
  node counts of a page of entries. All three are query shapes, not a query
  builder, in keeping with the port's existing style. `restore_subtree` is
  already there.
- `src/cyberfs/adapters/outbound/db/repositories.py` -- the listing (a
  `deleted_at IS NOT NULL` scan narrowed by "parent is absent or live"), the
  matching count, and the page-wide recursive aggregate.
- `src/cyberfs/adapters/outbound/db/models.py` + a migration -- one partial index
  on `(owner_id, deleted_at)` where `deleted_at IS NOT NULL`. The existing
  `ix_nodes_deleted_at` is not owner-scoped; it serves the retention sweep, which
  reads across all users. No new column.
- `src/cyberfs/domain/nodes.py` -- `TRASH_PURGE_NODE_BUDGET`, the ceiling on how
  many nodes one empty-trash call may destroy. A limit, so a constant here rather
  than a setting, like the tag and metadata limits beside it.
- `src/cyberfs/domain/errors.py` -- `TrashCountMismatchError(ConflictError)`,
  code `trash_count_mismatch`, so a client can tell the guard refusing it from
  any other `409`.
- `src/cyberfs/application/nodes.py` -- a `trash` read use case, `empty_trash`
  (which reuses `application/purge.py` rather than re-deriving the destructive
  sequence), the `NODE_PURGED` emission shared between `purge` and `empty_trash`,
  and one correction to the inherited cascading restore: it must invalidate the
  rows it lifted, not only the entry.
- `src/cyberfs/adapters/inbound/api/` -- `GET /api/v1/trash`,
  `POST /api/v1/trash/purge`, and their schemas.
- `src/cyberfs/domain/audit.py` -- `TRASH_EMPTIED`, deliberately **not** added to
  `ACTIVITY_ACTIONS`, so it lands in `SECURITY_ACTIONS` and is retained. A bulk
  irreversible destruction must stay attributable after the activity records
  around it have been pruned.
- `tests/unit/`, `tests/integration/`, `tests/e2e/`.

**Emptying the trash is irreversible and it is bulk.** The guards are: it touches
only nodes already deliberately deleted; the caller must supply the entry count
the trash currently holds, which is refused on mismatch, so a stale or unread
client cannot fire it; the work is bounded by nodes destroyed, not by entries, so
no single call can strip an unbounded tree; every entry destroyed produces the
same retained `NODE_PURGED` security record an individual purge of that entry
would, plus one `TRASH_EMPTIED` naming the batch. It cannot reach a live node --
it purges nothing that is not already trashed.

**`purge_subtree` emits no audit records**, and neither does `purge_one`
(`application/purge.py`): the only `NODE_PURGED` emitter is `NodeService.purge`,
one record per call. So `empty_trash` emits its own per-entry records, through
the same helper as `purge`, and purge audit granularity stays one record per
entry.

**Existing deployments may see new trash entries.** Any tree restored *before*
the cascading restore landed has descendants still sitting with `deleted_at` set;
the listing's collapse rule makes them their own entries, so they become visible
and restorable rather than invisible and doomed. That is a strict improvement,
but it means an operator may see trash entries for deletions they thought were
resolved. `docs/operations.md` should say so.

**The trash listing reveals nothing new.** It is owner-scoped, and an owner can
read the names, sizes, and paths of their own nodes already. It stays off the
administrative surface, where node names are gated behind `ADMIN_SHOW_FILENAMES`
and enabling that gate is audited; a trash listing for administrators would be
an ungated channel for exactly the thing that gate exists to withhold.

**No new configuration.** `TRASH_RETENTION_DAYS` and `PAGE_SIZE_MAX` already
exist and are what the listing reports and bounds itself by; the purge budget is
a domain constant. `TRASH_EMPTIED` is a new value in an existing audit-action
column; the only schema change is one index.
