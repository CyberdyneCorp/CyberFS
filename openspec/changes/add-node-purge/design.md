## Context

Soft delete sets `deleted_at` on a node and, via `NodeRepository.soft_delete_subtree`,
on every descendant individually. `PurgeJob` (`application/jobs.py:93`) then sweeps
trashed nodes past `TRASH_RETENTION_DAYS` and for each one:

1. reads the node, adjusts the owner's quota with `usage.purge_from_trash(...)`
2. deletes every version's stored object and version row
3. `grants.delete_for_node`, `keys.delete_data_keys_for_node`
4. `nodes.delete_permanently`

So the destructive sequence already exists and is exercised. What is missing is a
caller-facing way to run it for one node now, rather than waiting a month.

## Goals / Non-Goals

**Goals:**

- On-demand purge of a trashed node, freeing its quota in the same request.
- One implementation of the destructive sequence, shared by the timer and the
  endpoint, so they cannot diverge.
- No stranded objects: after purging a folder, nothing remains in the object store
  that no metadata row references.

**Non-Goals:**

- "Empty my whole trash" in one call. A per-node purge composes into that from a
  trash listing, and an endpoint that destroys everything a user has deleted
  deserves its own consideration.
- A trash listing endpoint. The spec already requires a trash view; whether one is
  exposed is separate from this change.
- Changing `DELETE /api/v1/nodes/{node_id}`, which stays a soft delete.
- Purging individual *versions*. `VERSION_RETENTION_COUNT` already bounds those.
- Any guarantee about backups. Purge destroys the live copy; what a prior backup
  holds is out of scope and `docs/restore-runbook.md` governs it.

## Decisions

**`POST .../purge`, not `DELETE ...?permanent=true`.** A flag on the existing
delete route would make the difference between recoverable and irreversible a
query parameter that is easy to set by accident and easy to miss in a log. A
distinct path is explicit at the call site and in the access log.

**Refuse a live node with `409` rather than trashing-then-purging.** Auto-trashing
would collapse two deliberate steps into one and make a single mistaken call
unrecoverable. `409 Conflict` says the resource is not in a state where this
applies, which is exactly the situation. This is the main safety property of the
design, so it belongs in the spec rather than only in code.

**Factor the sequence out of `PurgeJob` into a shared helper** that both the job
and the use case call, rather than having the use case re-implement it or call the
job. Re-implementing invites drift precisely where drift means either a quota leak
or an orphaned object; calling the job would mean running its batch sweep as a side
effect of purging one node.

**Walk the subtree and delete objects explicitly.** Relying on the database cascade
would be less code but would strand every descendant's objects, because
`delete_permanently` on a folder removes descendant rows in one statement and the
object store has no idea. `OrphanReaper` would collect them eventually, which is
acceptable for the timer path but not for an operation whose entire purpose is to
free space now. The walk is needed regardless to sum the bytes for the quota
release.

**Charge the quota release to the owner, not the caller.** An administrator
purging someone else's node must free *that user's* space. `PurgeJob` already does
this by reading `node.owner_id`, so the shared helper keeps it and the endpoint
inherits it -- worth stating because "the caller" is the wrong default here and an
easy mistake.

**`NODE_PURGED` needs no classification work.** `SECURITY_ACTIONS` is derived as
everything not in `ACTIVITY_ACTIONS` (`domain/activity.py:46`), so a new action is
retained by default. That is the safe direction and the reason the set is derived;
this change relies on it rather than adding an entry.

**`404` for unknown or already-purged, not `204`.** Reporting success for a node
that does not exist would make a typo'd id indistinguishable from a real purge in
an audit trail. Idempotency is not worth that ambiguity for a destructive call.

## Risks / Trade-offs

- **Irreversible data loss from a mistaken call.**
  → Mitigation: the must-be-trashed precondition means the content was already
  deliberately deleted; the endpoint is a separate path from delete; the operation
  is recorded as a retained security record with the actor.

- **An administrator can destroy any user's trashed content.** This is the
  requested scope, but it is real reach.
  → Mitigation: same audit record, naming the administrator as actor and the owner
  separately, so the action is attributable and reviewable in the audit log.

- **A partial failure mid-subtree** -- some objects deleted, then the object store
  errors -- leaves metadata referring to objects that are gone.
  → Mitigation: the deletions run inside the existing unit of work and commit once
  at the end, so metadata survives a failure and the purge can be retried.
  Already-deleted objects make the retry a no-op rather than an error. The
  inverse ordering (rows first, objects second) would strand objects instead, which
  `OrphanReaper` can fix but a user cannot see.

- **A large subtree makes one slow request.** The timer path batches by
  `PAGE_SIZE_MAX`; an on-demand purge of a deep tree has no such bound.
  → Mitigation: accepted for this change, and worth measuring. If it becomes a
  problem the answer is the same asynchronous pattern `ASYNC_REWRAP_THRESHOLD_NODES`
  already uses for large shared subtrees, not a hidden cap that silently purges part
  of a tree.

## Migration Plan

Additive. `NODE_PURGED` is a new value in an existing audit-action column, so no
schema migration. Rollback is removing the route; nothing already purged comes
back, and `PurgeJob` is untouched throughout.

## Open Questions

- Should purge be refused while an async rewrap is in flight for that subtree? The
  rewrap worker would find its nodes gone, which is probably benign but has not
  been checked. Worth confirming during implementation.
