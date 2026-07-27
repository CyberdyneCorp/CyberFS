## Why

Purge exists but is only reachable on a timer. `PurgeJob` deletes trashed nodes
once they pass `TRASH_RETENTION_DAYS` (30 by default), and nothing can trigger it
sooner. So a user who deletes a large file to free space sees no change in their
quota for a month, and there is no way to destroy content on request -- which is
what a deletion request under a data-protection regime actually asks for.

The gap has a concrete cost already: the live end-to-end suite trashes its scratch
folder on teardown because that is the strongest deletion the API offers, so every
run leaves a tree occupying quota for 30 days.

## What Changes

- `POST /api/v1/nodes/{node_id}/purge` permanently destroys a trashed node: its
  metadata, every version's stored object, its wrapped data keys, its grants, and
  its public links. Quota is released immediately.
- **The node must already be in the trash.** A live node is refused with `409`.
  Destroying content takes two deliberate steps, and a mistaken single call cannot
  lose data that was never deleted.
- Purging a folder purges its whole subtree, since soft delete already trashes
  descendants individually.
- The owner may purge their own trashed nodes; an administrator may purge anyone's.
- The operation is recorded as a **security** audit record, which is retained and
  never pruned -- permanent destruction must stay attributable after the activity
  records around it have aged out.

Not changing: `PurgeJob` and its 30-day sweep continue unchanged as the backstop,
and `DELETE /api/v1/nodes/{node_id}` keeps its current soft-delete meaning.

## Capabilities

### New Capabilities

None. This exposes an operation the `file-storage` capability already specifies.

### Modified Capabilities

- `file-storage`: "Soft delete, restore, and purge" currently describes purge only
  as something retention expiry does. It gains on-demand purge, the
  must-be-trashed precondition, subtree behaviour, who may call it, and immediate
  quota release.

## Impact

**Affected code:**

- `src/cyberfs/application/nodes.py` -- a `purge_node` use case. The per-node
  steps are already in `PurgeJob._purge_node` / `_purge_objects`; the shared
  sequence should be factored out rather than duplicated, so the timer and the
  endpoint cannot drift apart.
- `src/cyberfs/adapters/inbound/api/routers/` -- the route, its authorization, and
  the `409` on a live node.
- `src/cyberfs/domain/audit.py` -- a `NODE_PURGED` action, which lands in
  `SECURITY_ACTIONS` automatically since that set is derived from everything not
  classified as activity.
- `tests/unit/`, `tests/integration/`, `tests/e2e/` -- coverage, plus switching
  the e2e teardown to purge so the suite stops leaving trash behind.

**Object-store correctness.** Every child table cascades on `node_id`
(`FileVersionRow`, `GrantRow`, `PublicLinkRow`), and `NodeRow.parent_id` cascades
too. Deleting a folder's row therefore removes all descendant rows in one
statement -- and would strand every descendant's objects in MinIO with no metadata
pointing at them. `OrphanReaper` would eventually collect them, but an operation
whose stated purpose is to free space must not depend on a later sweep to actually
free it. The use case must walk the subtree and delete objects explicitly, which
it needs to do anyway to sum the bytes for the quota release.

**This is irreversible.** Purged content is not recoverable from CyberFS, and
`docs/restore-runbook.md` makes no guarantee that a backup taken before the purge
still exists or contains it. That is the intent of the operation; the
must-be-trashed precondition is the guard.

**No new configuration.** No environment variable, no migration -- `NODE_PURGED`
is a new enum value in an existing audit action column.
