## 1. Domain

- [ ] 1.1 Add `TRASH_EMPTIED` to `AuditAction` in `src/cyberfs/domain/audit.py`
- [ ] 1.2 Deliberately do NOT add it to `ACTIVITY_ACTIONS`, so `SECURITY_ACTIONS` (the derived complement) retains it; state the reason in a comment next to the entry so a later reader does not "fix" the omission
- [ ] 1.3 Confirm `TRASH_EMPTIED` feeds no `SUMMARY_BUCKETS` counter — the per-node `NODE_PURGED` records already describe what happened, and counting the batch too would double-count in the activity rollup
- [ ] 1.4 Define the trash-entry value object (node, original path, deleted-at, purge-after, subtree bytes, subtree node count) where the other node value objects live, so the API layer serializes rather than computes

## 2. Persistence

- [ ] 2.1 Add `list_trash_entries(owner_id, *, limit, cursor)` to the `NodeRepository` port: trashed nodes owned by that user whose parent is absent or not trashed, most recently deleted first, deterministic tie-break, cursor-paginated
- [ ] 2.2 Add a page-wide aggregate to the port returning subtree bytes and node counts for a set of entry ids in one query, so a page costs one aggregate rather than one recursive walk per entry
- [ ] 2.3 Add `restore_subtree(node_id, deleted_at, now)` to the port: clear `deleted_at` on the node and on every descendant whose `deleted_at` equals the entry's, bumping each row's revision, mirroring `soft_delete_subtree`
- [ ] 2.4 Implement all three in `SqlNodeRepository`; the "parent not trashed" test belongs in the `WHERE` clause, not after the `LIMIT`, or pages come back short while entries remain
- [ ] 2.5 Comment at the timestamp-equality predicate why it identifies a delete batch (`soft_delete_subtree` writes one `now` across the whole `UPDATE`) so it is not mistaken for an approximation
- [ ] 2.6 Add the partial index on `(owner_id, deleted_at)` where `deleted_at IS NOT NULL` to `NodeRow.__table_args__`, with a comment distinguishing it from `ix_nodes_deleted_at`, which is deliberately not owner-scoped because the sweep crosses users
- [ ] 2.7 Write the Alembic migration for the index and verify `alembic upgrade head`; note in the task whether `downgrade` was exercised, as the metadata change did
- [ ] 2.8 Implement the three new methods on `FakeNodeRepository` in `tests/unit/fakes.py`, keeping the collapse and timestamp rules faithful so unit tests exercise the real rules and not a laxer fake

## 3. Use cases

- [ ] 3.1 `NodeService.trash(...)` in `application/nodes.py`: owner-scoped read, page bounded by `page_size_max`, entries hydrated with path, totals, and purge-after computed from `TRASH_RETENTION_DAYS`
- [ ] 3.2 Fix `NodeService.restore` to call `restore_subtree`, so the rows match the bytes it already moves out of the trashed bucket
- [ ] 3.3 Keep exactly one `NODE_RESTORED` audit record, for the entry, matching how `delete` records one `NODE_DELETED` for a subtree
- [ ] 3.4 Keep the single `_ensure_name_free` check at the entry, with a comment on why descendants need none (a trashed folder cannot gain live children, because creating one authorizes the parent and `_authorize` refuses a trashed node)
- [ ] 3.5 `NodeService.empty_trash(...)`: owner-scoped, refuse with `ConflictError` unless the stated entry count equals the current one, then purge entries oldest deletion first through `application/purge.py`, bounded at `page_size_max` entries, returning what was destroyed and how many entries remain
- [ ] 3.6 Emit `TRASH_EMPTIED` once for the batch with entry count and reclaimed bytes, in addition to the per-node `NODE_PURGED` records `purge_subtree` already produces
- [ ] 3.7 Do NOT re-implement the destructive sequence — `purge_one`/`purge_subtree` are shared by the sweep and the per-node endpoint precisely so it exists once
- [ ] 3.8 Invalidate cached listings and permissions on a subtree restore the way `delete` does (`reparented=True`), and do not cache the trash listing itself

## 4. API

- [ ] 4.1 `GET /api/v1/trash` returning a paginated page of trash entries, `limit`/`cursor` shaped like the other listings
- [ ] 4.2 `POST /api/v1/trash/purge` taking the expected entry count, returning bytes reclaimed, objects deleted, nodes destroyed, and entries remaining; document `409` on a count mismatch in the route's `responses`
- [ ] 4.3 Schemas for both, carrying no digest and no object key — a trash entry is metadata about a node, not a handle on its content
- [ ] 4.4 Confirm neither route appears under `/api/v1/admin/*` and that no admin response gains a node name, path, or digest
- [ ] 4.5 Confirm both routes and their schemas appear in the OpenAPI document (`just openapi`)

## 5. Unit tests (fakes, no I/O — the rules, not the storage)

- [ ] 5.1 A trashed file appears as an entry; a live file never does; a root folder never does
- [ ] 5.2 Trashing a folder with descendants yields exactly one entry
- [ ] 5.3 A node trashed inside an already-trashed folder is not an entry; it becomes one once its parent is restored
- [ ] 5.4 An entry's reported bytes and node count cover the whole subtree, not the folder's own zero
- [ ] 5.5 An entry's reported path is the one the node occupied, derived from its live ancestors
- [ ] 5.6 An entry's purge-after equals its deletion time plus `TRASH_RETENTION_DAYS`
- [ ] 5.7 Another user's trashed node never appears in the caller's listing; a former share recipient sees nothing
- [ ] 5.8 Restoring a folder clears `deleted_at` on every descendant that deletion trashed
- [ ] 5.9 A node deleted separately before its parent stays trashed when the parent is restored, and then appears as its own entry
- [ ] 5.10 Restore moves out of the trashed bucket exactly the bytes of the nodes it made visible
- [ ] 5.11 Every restored row's revision advanced, so a pre-delete `If-Match` on a descendant is refused
- [ ] 5.12 Restoring a node whose parent is still trashed lands it in the owner's root folder
- [ ] 5.13 `empty_trash` with a matching count purges every entry and releases the bytes
- [ ] 5.14 `empty_trash` with a stale count raises a conflict and destroys nothing
- [ ] 5.15 `empty_trash` on an empty trash with a count of zero succeeds, destroying nothing
- [ ] 5.16 `empty_trash` stops at the bound and reports the remaining entry count
- [ ] 5.17 `empty_trash` emits one `TRASH_EMPTIED` plus one `NODE_PURGED` per node destroyed
- [ ] 5.18 `TRASH_EMPTIED` is in `SECURITY_ACTIONS` and not in `ACTIVITY_ACTIONS`, and an activity prune leaves it in place
- [ ] 5.19 `empty_trash` requested by a non-owner touches nothing belonging to anyone else

## 6. Integration tests (real Postgres/Redis/MinIO, marked `integration`, run in CI)

Everything here needs real infrastructure: `FakeUnitOfWork` models no foreign
keys, so no cascade, no partial index, and no recursive query can be proven
against it.

- [ ] 6.1 `GET /api/v1/trash` round-trips: upload, delete, list, restore by the identifier the listing supplied, confirm the file downloads again — the loop the spec requires and the API could not previously complete
- [ ] 6.2 A deleted folder of files yields one entry against real Postgres, with the subtree totals produced by the aggregate query rather than the fake
- [ ] 6.3 Pagination: more entries than the page size returns full pages and a working cursor, with no short page while entries remain
- [ ] 6.4 Cross-user isolation over the API: Bob's trash never shows Alice's nodes, and a recipient whose grant existed at delete time sees nothing
- [ ] 6.5 Restoring a folder makes every descendant listable and downloadable again, verified through the API rather than the repository
- [ ] 6.6 A separately deleted child stays trashed after its parent is restored, then restores independently
- [ ] 6.7 Reported quota after delete-then-restore matches the reconciliation job's recomputation — the drift this change fixes, provable only against real rows
- [ ] 6.8 `POST /api/v1/trash/purge` empties the trash, releases the quota, and leaves no object in MinIO that no metadata row references
- [ ] 6.9 A stale entry count returns `409` and the trash is untouched afterwards
- [ ] 6.10 Emptying a trash containing a shared, encrypted, multi-version file removes its grants, wrapped keys, version rows, tags, and metadata — the FK cascades, which only real Postgres can demonstrate
- [ ] 6.11 The migration applies and the partial index exists and is used by the listing (`EXPLAIN`), so the trash read does not become a full scan as trash grows
- [ ] 6.12 Trashed nodes remain absent from `GET /nodes/{id}/children`, `GET /search`, and the S3 listing — the invariants this change must not loosen

## 7. End-to-end tests (`tests/e2e`, against a live deployment, marked `e2e`)

- [ ] 7.1 Against the deployment: upload, delete, find the file in `GET /api/v1/trash`, restore it, download it, then clean up by purging
- [ ] 7.2 Against the deployment: create a small folder tree, delete it, confirm one entry with plausible totals, empty the trash with the stated count, and confirm reported usage drops
- [ ] 7.3 Simplify the existing e2e teardown to empty the trash once instead of purging per node, so the suite exercises the new path it now depends on

## 8. Verification and documentation

- [ ] 8.1 `just lint`, `just typecheck`, `just test-unit` clean
- [ ] 8.2 `just test-integration` clean, verified in CI rather than assumed; record the run and the test counts before and after so it is evident the new tests actually ran
- [ ] 8.3 `just test-e2e` clean against the deployment
- [ ] 8.4 Measure the trash listing on a tree with a few hundred trashed nodes and record the numbers, since the page-wide aggregate is the one read whose cost this change introduces
- [ ] 8.5 Document the trash in `README.md` and `docs/api.md`: the listing, that entries are per deletion, that restore returns the subtree, and the empty-trash loop with its count guard and its bound
- [ ] 8.6 Note in `docs/operations.md` that deployments upgrading may see trash entries appear for descendants stranded by earlier folder restores, and that the reconciliation job converges the quota buckets
- [ ] 8.7 Add `trash.emptied` to the activity action table in `docs/activity.md`, marked as a retained security record rather than activity
- [ ] 8.8 Run `openspec validate add-trash-view --strict`
