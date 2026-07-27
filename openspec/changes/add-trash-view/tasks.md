## 0. Inherited from `main` — do not re-implement

Read `application/nodes.py` `delete`/`restore` and
`adapters/outbound/db/repositories.py` `restore_subtree` before starting. The
cascading restore and its quota arithmetic are already correct and already
specified, so nothing below touches them:

- `NodeRepository.restore_subtree(node_id, now)` exists on the port, in
  `SqlNodeRepository`, and in `tests/unit/fakes.py`; it lifts the entry plus every
  descendant sharing its `deleted_at`, bumps each revision, and returns the rows
  it cleared — including the comment explaining why timestamp equality identifies
  a delete batch.
- `NodeService.restore` calls it and moves out of the trashed bucket only the
  bytes of the rows it returned. `_subtree_bytes` has been deleted.
- `soft_delete_subtree` returns the rows it moved and `delete` charges only those.
- `restore` emits exactly one `NODE_RESTORED`, and `_ensure_name_free` is checked
  once, at the entry.
- Pinned by `test_restore_brings_back_the_deleted_subtree`,
  `test_restore_returns_the_subtree_bytes_to_the_live_bucket`,
  `test_a_child_deleted_before_its_parent_stays_trashed`,
  `test_restore_advances_the_revision_of_every_row_it_lifts`.

The one correction this change does make to restore is its cache invalidation
(task 3.10), because restore became a subtree mutation and the invalidation stayed
entry-shaped.

## 1. Domain

- [x] 1.1 Add `TRASH_EMPTIED` to `AuditAction` in `src/cyberfs/domain/audit.py`
- [x] 1.2 Deliberately do NOT add it to `ACTIVITY_ACTIONS`, so `SECURITY_ACTIONS` (the derived complement) retains it; state the reason in a comment next to the entry so a later reader does not "fix" the omission
- [x] 1.3 Confirm `TRASH_EMPTIED` feeds no `SUMMARY_BUCKETS` counter, for the same reason `NODE_PURGED` feeds none: the rollup counts the soft delete the user performed (`deletions`), and counting the later destruction of something already counted would report one user action twice
- [x] 1.4 Define the trash-entry value object (node, original path, deleted-at, purge-after, subtree bytes, subtree node count) where the other node value objects live, so the API layer serializes rather than computes; deliberately no digest and no effective permission — see the delta's "Trash listing" requirement
- [x] 1.5 Add `TRASH_PURGE_NODE_BUDGET` to `src/cyberfs/domain/nodes.py` beside the tag and metadata limits, with a comment that it bounds nodes and not entries because an entry is a subtree root of unbounded size
- [x] 1.6 Add `TrashCountMismatchError(ConflictError)` with code `trash_count_mismatch` to `src/cyberfs/domain/errors.py`, so a client can tell a stale count from every other `409` this API returns; keep the module docstring's list of codes current

## 2. Persistence

- [x] 2.1 Add `list_trash_entries(owner_id, *, limit, cursor, oldest_first=False)` to the `NodeRepository` port: trashed nodes owned by that user whose parent is absent or not trashed, deterministic tie-break, cursor-paginated. `empty_trash` needs the oldest first and the listing needs the newest first; one method with an explicit order, so the two cannot drift into two different definitions of "entry"
- [x] 2.2 Add `count_trash_entries(owner_id)` to the port, returning the total the listing reports and the purge guard checks
- [x] 2.3 Add a page-wide aggregate to the port returning subtree bytes and node counts for a set of entry ids in one query, so a page costs one aggregate rather than one recursive walk per entry
- [x] 2.4 Implement all three in `SqlNodeRepository`, with the "parent not trashed" predicate built once and shared by the listing and the count — the guard refusing a caller must be checking the same set the listing showed them
- [x] 2.5 The "parent not trashed" test belongs in the `WHERE` clause, not after the `LIMIT`, or pages come back short while entries remain
- [x] 2.6 Add the partial index on `(owner_id, deleted_at)` where `deleted_at IS NOT NULL` to `NodeRow.__table_args__`, with a comment distinguishing it from `ix_nodes_deleted_at`, which is deliberately not owner-scoped because the sweep crosses users
- [ ] 2.7 Write the Alembic migration for the index and verify `alembic upgrade head`; note in the task whether `downgrade` was exercised, as the metadata change did **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [x] 2.8 Implement the three new methods on `FakeNodeRepository` in `tests/unit/fakes.py`, keeping the collapse rule and the ordering faithful so unit tests exercise the real rules and not a laxer fake

## 3. Use cases

- [x] 3.1 `NodeService.trash(...)` in `application/nodes.py`: owner-scoped read, page bounded by `page_size_max`, entries hydrated with path, totals, and purge-after computed from `TRASH_RETENTION_DAYS`, plus the total entry count
- [x] 3.2 Return the total from the same use case as the page, not a second endpoint — it is the input to the purge guard, so a caller must not have to make two requests that can disagree
- [x] 3.3 Do not cache the trash listing; a stale trash hides a restorable entry or offers one that is already gone
- [x] 3.4 `NodeService.empty_trash(...)`: owner-scoped; count the entries and refuse with `TrashCountMismatchError` unless the stated count equals it; then take entries oldest deletion first and purge each through `application/purge.py`
- [x] 3.5 Spend the node budget with the aggregate from task 2.3, not blindly: stop *before* starting an entry whose node count would push the call past `TRASH_PURGE_NODE_BUDGET`, so no entry is ever left partly destroyed — except that if nothing has been destroyed yet the oldest entry is destroyed whatever its size, or a trash whose oldest entry exceeds the budget could never be emptied by any sequence of calls
- [x] 3.6 Return what was destroyed (bytes, objects, nodes, entries) and how many entries remain, so the client's next call can state the number the response just reported
- [x] 3.7 Count and purge inside the same unit of work, with a comment: a node trashed concurrently either lands before the count (and is destroyed) or after the commit (and is reported as remaining) — never half of either
- [x] 3.8 Emit one `NODE_PURGED` per entry, identical in shape to what `NodeService.purge` writes today, plus one `TRASH_EMPTIED` for the batch carrying the entry count and reclaimed bytes. `purge_one` and `purge_subtree` emit **no** audit records — the only `NODE_PURGED` emitter is `NodeService.purge` — so `empty_trash` must emit its own. Extract that emission into one helper both call, in the application layer, so purge audit granularity cannot drift; `purge.py` stays audit-free because its other caller is the sweep, which has no actor to attribute
- [x] 3.9 Do NOT re-implement the destructive sequence — `purge_one`/`purge_subtree` are shared by the sweep and the per-node endpoint precisely so it exists once
- [x] 3.10 Fix the inherited restore's invalidation: it drops the entry's node key, its parent's listing prefix, and all permission decisions, but a subtree restore also mutates every descendant row. Invalidate the node key of every row `restore_subtree` returned and the entry's own listing prefix, as `caching/spec.md` "Invalidation on mutation" requires of a subtree mutation. `restore_subtree` already returns those rows, so this iterates a list already in hand

## 4. API

- [x] 4.1 `GET /api/v1/trash` returning a paginated page of trash entries with `limit`/`cursor` shaped like the other listings, and `total_entries` in the envelope beside `items` and `next_cursor`
- [x] 4.2 `POST /api/v1/trash/purge` taking the expected entry count, returning bytes reclaimed, objects deleted, nodes destroyed, entries destroyed, and entries remaining; document `409 trash_count_mismatch` in the route's `responses`
- [x] 4.3 Document `409 name_taken` in `POST /api/v1/nodes/{node_id}/restore`'s `responses` — the refusal exists already but was unreachable while no caller could learn a trashed id, and a trash listing makes it a routine outcome
- [x] 4.4 Schemas for both new routes, carrying no digest and no object key — a trash entry is a restore-or-purge handle, not a handle on content
- [x] 4.5 Confirm neither route appears under `/api/v1/admin/*` and that no admin response gains a node name, path, or digest
- [x] 4.6 Confirm both routes and their schemas appear in the OpenAPI document (`just openapi`)

## 5. Unit tests (fakes, no I/O — the rules, not the storage)

- [x] 5.1 A trashed file appears as an entry; a live file never does; a root folder never does
- [x] 5.2 Trashing a folder with descendants yields exactly one entry
- [x] 5.3 A node trashed inside an already-trashed folder is not an entry; it becomes one once its parent is restored — the listing half of the inherited `test_a_child_deleted_before_its_parent_stays_trashed`
- [x] 5.4 An entry's reported bytes and node count cover the whole subtree, not the folder's own zero
- [x] 5.5 An entry's reported path is the one the node occupied, derived from its live ancestors
- [x] 5.6 An entry's purge-after equals its deletion time plus `TRASH_RETENTION_DAYS`
- [x] 5.7 The reported total counts every entry, not the page — more entries than the page size still reports the full total
- [x] 5.8 Another user's trashed node never appears in the caller's listing; a former share recipient sees nothing
- [x] 5.9 Restoring an entry whose name a live sibling has taken raises `NameTakenError` and lifts nothing — assert a descendant is still trashed afterwards, not just the entry. This pins behaviour that already exists but was unreachable, so it is a characterization test, not a regression test; no revert-and-fail proof applies
- [x] 5.10 Restoring a node whose parent is still trashed lands it in the owner's root folder — likewise existing behaviour, untested today
- [x] 5.11 A subtree restore invalidates the node key of every row it lifted and the entry's listing prefix. This one *is* a regression test for task 3.10: revert the invalidation change, watch it fail, restore it, and say so in the PR
- [x] 5.12 `empty_trash` with a matching count purges every entry and releases the bytes
- [x] 5.13 `empty_trash` with a stale count raises `TrashCountMismatchError` and destroys nothing
- [x] 5.14 `empty_trash` on an empty trash with a count of zero succeeds, destroying nothing
- [x] 5.15 `empty_trash` stops before an entry that would exceed `TRASH_PURGE_NODE_BUDGET`, leaves that entry entirely intact, and reports it as remaining
- [x] 5.16 `empty_trash` destroys an oldest entry that on its own exceeds the budget, rather than refusing and stalling forever
- [x] 5.17 `empty_trash` emits exactly one `TRASH_EMPTIED` and one `NODE_PURGED` per *entry* destroyed — not one per node — with the same context shape `NodeService.purge` produces
- [x] 5.18 `TRASH_EMPTIED` is in `SECURITY_ACTIONS` and not in `ACTIVITY_ACTIONS`, and an activity prune leaves it in place
- [x] 5.19 `empty_trash` requested by a caller touches only their own entries; another owner's trashed nodes survive and their quota is unchanged

## 6. Integration tests (real Postgres/Redis/MinIO, marked `integration`, run in CI)

Everything here needs real infrastructure: `FakeUnitOfWork` models no foreign
keys, so no cascade, no partial index, and no recursive query can be proven
against it.

- [ ] 6.1 `GET /api/v1/trash` round-trips: upload, delete, list, restore by the identifier the listing supplied, confirm the file downloads again — the loop the spec requires and the API could not previously complete **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.2 A deleted folder of files yields one entry against real Postgres, with the subtree totals produced by the aggregate query rather than the fake **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.3 Pagination: more entries than the page size returns full pages, a working cursor, and the same `total_entries` on every page, with no short page while entries remain **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.4 Cross-user isolation over the API: Bob's trash never shows Alice's nodes, and a recipient whose grant existed at delete time sees nothing **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.5 Restoring a folder makes every descendant listable and downloadable again, verified through the API rather than the repository **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.6 A separately deleted child stays trashed after its parent is restored, then appears in the listing as its own entry and restores independently **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.7 Delete a file, create a new one with the same name in the same parent, then restore the first: `409 name_taken`, and the trashed subtree is still entirely trashed. Rename the live occupant and the restore then succeeds — the resolution `docs/api.md` documents **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.8 Reported quota after delete-then-restore matches the reconciliation job's recomputation, proved against real rows **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.9 `POST /api/v1/trash/purge` with the count the listing reported empties the trash, releases the quota, and leaves no object in MinIO that no metadata row references **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.10 A stale entry count returns `409 trash_count_mismatch` and the trash is untouched afterwards **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.11 A trash whose nodes exceed `TRASH_PURGE_NODE_BUDGET` needs two calls: the first reports entries remaining, the second (with the reported count) finishes, and no entry is ever observed half-destroyed **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.12 Emptying a trash containing a shared, encrypted, multi-version file removes its grants, wrapped keys, version rows, tags, and metadata — the FK cascades, which only real Postgres can demonstrate **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.13 The migration applies and the partial index exists — query `pg_indexes`, which is stable in CI. No `EXPLAIN` assertion here: on a freshly migrated database with a handful of rows the planner correctly prefers a sequential scan, so a plan assertion would fail, or pass for the wrong reason and become a test the suite learns to ignore. The plan is inspected in task 8.4 instead **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 6.14 Trashed nodes remain absent from `GET /nodes/{id}/children`, `GET /search`, and the S3 listing — the invariants this change must not loosen **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.

## 7. End-to-end tests (`tests/e2e`, against a live deployment, marked `e2e`)

`POST /api/v1/trash/purge` is deliberately **not** exercised here. The e2e tier
runs against a real account whose trash may hold data no test created and no test
can restore, and the count guard turns any concurrent trash into a `409` that
fails the whole session. Whole-trash emptying is proven in task 6.9 and 6.11,
where the database is disposable.

- [ ] 7.1 Against the deployment: upload, delete, find the file in `GET /api/v1/trash`, restore it, download it, then clean up by purging that id **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 7.2 Against the deployment: create a small folder tree, delete it, confirm it appears as one entry with plausible subtree totals and that `total_entries` is at least one, then restore it and clean up by purging the scratch id — no whole-trash operation **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 7.3 Leave `tests/e2e/conftest.py`'s teardown as it is: it trashes and purges exactly one identifiable scratch folder by id. Do not replace it with an empty-trash call — that would destroy unrelated trash in a live account and make teardown depend on the feature under test **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.

## 8. Verification and documentation

- [x] 8.1 `just lint`, `just typecheck`, `just test-unit` clean
- [ ] 8.2 `just test-integration` clean, verified in CI rather than assumed; record the run and the test counts before and after so it is evident the new tests actually ran **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 8.3 `just test-e2e` clean against the deployment **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [ ] 8.4 Measure the trash listing on a tree with a few hundred trashed nodes and record the numbers — the page aggregate and the `total_entries` count are the two reads this change introduces. Inspect the plan here, on seeded data, with `SET LOCAL enable_seqscan = off` if the planner needs persuading, rather than asserting a plan in CI **Not verified here:** no Docker daemon in this environment, so this could not be executed. Written and awaiting CI.
- [x] 8.5 Document the trash in `README.md` and `docs/api.md`: the listing and its total, that entries are per deletion, that restore returns the subtree, that a restore can be refused `409 name_taken` and how to resolve it, and the empty-trash loop with its count guard and its node bound
- [x] 8.6 Note in `docs/operations.md` that deployments may see trash entries appear for descendants stranded by folder restores performed before the cascading restore landed, and that the reconciliation job converges the quota buckets
- [x] 8.7 Add `trash.emptied` to the activity action table in `docs/activity.md`, marked as a retained security record rather than activity
- [x] 8.8 Run `openspec validate add-trash-view --strict`
