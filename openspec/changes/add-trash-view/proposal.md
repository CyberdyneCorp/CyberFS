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

Closing the loop exposes a second contradiction immediately. Delete cascades:
`soft_delete_subtree` trashes a folder and every descendant. **Restore does
not** -- `NodeService.restore` clears `deleted_at` on one row. Restoring a
folder therefore brings back an empty folder and leaves its contents trashed and
(today) unreachable forever. The quota code already assumes otherwise: restore
sums the whole subtree with `include_deleted=True` and moves all of those bytes
out of the trashed bucket, so reported usage counts descendants as live while
their rows say trashed, until the reconciliation job disagrees. A trash listing
that offers "restore" on a folder has to actually restore the folder.

## What Changes

- **`GET /api/v1/trash`** lists the caller's trashed nodes, newest deletion
  first, paginated like every other listing.
- **One entry per delete, not one per row.** A trash entry is a trashed node
  whose parent is not itself trashed. Deleting a 400-node folder produces one
  entry, not 400. Each entry reports its original path, when it was deleted, when
  the retention sweep will destroy it, and the total bytes and node count it
  would restore -- so the entry is self-contained and the client never has to
  read a trashed node individually.
- **Restore restores the subtree.** Restoring an entry brings back every node
  that the same delete trashed, identified by the deletion timestamp they share.
  A node deleted separately *before* its parent stays deleted, and reappears as
  its own trash entry once its parent is live again.
- **`POST /api/v1/trash/purge`** empties the trash, guarded: the caller states
  how many entries they intend to destroy and the call is refused if the trash
  does not currently hold exactly that many, so a bulk irreversible operation
  cannot be issued by a client that has not looked. Bounded per call, reporting
  what remains.
- The trash is the owner's and nobody else's. A share recipient sees nothing --
  grants stop conferring access at delete. No administrator surface is added.

Not changing: `DELETE /api/v1/nodes/{node_id}` stays a soft delete;
`POST /api/v1/nodes/{node_id}/purge` keeps its shape, its must-be-trashed `409`,
and its owner-or-admin rule; `PurgeJob` and the 30-day sweep are untouched;
trashed nodes remain absent from folder listings, search, and the S3 surface.

## Capabilities

### New Capabilities

None. This implements a `file-storage` requirement that already exists and fixes
a restore that already contradicts it.

### Modified Capabilities

- `file-storage`: "Soft delete, restore, and purge" gains the cascading restore,
  the timestamp rule that decides what a restore takes with it, and the quota
  consequence. Two requirements are added -- "Trash listing" and "Emptying the
  trash" -- rather than folding a caller-facing listing into "Listing, search,
  and metadata", whose whole subject is the *live* tree.

## Impact

**Affected code:**

- `src/cyberfs/domain/ports/repositories.py` -- `NodeRepository` gains a
  trash-entry listing and an aggregate for the bytes and node counts of a page of
  entries. Both are query shapes, not a query builder, in keeping with the port's
  existing style.
- `src/cyberfs/adapters/outbound/db/repositories.py` -- the listing (a
  `deleted_at IS NOT NULL` scan narrowed by "parent is absent or live"), the
  page-wide recursive aggregate, and a subtree restore that clears `deleted_at`
  by shared timestamp.
- `src/cyberfs/adapters/outbound/db/models.py` + a migration -- one partial index
  on `(owner_id, deleted_at)` where `deleted_at IS NOT NULL`. The existing
  `ix_nodes_deleted_at` is not owner-scoped; it serves the retention sweep, which
  reads across all users. No new column.
- `src/cyberfs/application/nodes.py` -- a `trash` read use case, a corrected
  `restore`, and `empty_trash`, which reuses `application/purge.py` rather than
  re-deriving the destructive sequence.
- `src/cyberfs/adapters/inbound/api/` -- `GET /api/v1/trash`,
  `POST /api/v1/trash/purge`, and their schemas.
- `src/cyberfs/domain/audit.py` -- `TRASH_EMPTIED`, deliberately **not** added to
  `ACTIVITY_ACTIONS`, so it lands in `SECURITY_ACTIONS` and is retained. A bulk
  irreversible destruction must stay attributable after the activity records
  around it have been pruned.
- `tests/unit/`, `tests/integration/`, `tests/e2e/`.

**Emptying the trash is irreversible and it is bulk.** The guards are: it touches
only nodes already deliberately deleted; the caller must supply the entry count
it expects, which is refused on mismatch, so a stale or unread client cannot
fire it; the work is bounded per call; every node destroyed produces the same
retained security record an individual purge would, plus one naming the batch.
It cannot reach a live node -- it purges nothing that is not already trashed.

**Restoring a folder changes behaviour for existing deployments.** Any tree
restored before this change has descendants still sitting with `deleted_at` set;
after the change they become their own trash entries and are restorable
individually. That is a strict improvement over invisible, but it means an
operator may see trash entries appear for deletions they thought were resolved.
`docs/operations.md` should say so.

**The trash listing reveals nothing new.** It is owner-scoped, and an owner can
read the names, sizes, and paths of their own nodes already. It stays off the
administrative surface, where node names are gated behind `ADMIN_SHOW_FILENAMES`
and enabling that gate is audited; a trash listing for administrators would be
an ungated channel for exactly the thing that gate exists to withhold.

**No new configuration.** `TRASH_RETENTION_DAYS` and `PAGE_SIZE_MAX` already
exist and are what the listing reports and bounds itself by. `TRASH_EMPTIED` is
a new value in an existing audit-action column; the only schema change is one
index.
