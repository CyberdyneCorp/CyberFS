## 1. Domain

- [ ] 1.1 Add a tag-delta validator in `src/cyberfs/domain/nodes.py`: normalize both the additions and the removals with `normalize_tag`, refuse blank and over-length entries as `validate_tags` does, and refuse a tag named in both directions
- [ ] 1.2 Add a metadata-delta validator: key and value length limits, no duplicate key within the request, and no key named in both directions. Refuse a reserved-prefix key in the set list *and* in the removal list, testing the prefix with `key.casefold().startswith(RESERVED_METADATA_PREFIX)` exactly as `validate_metadata` does; ordinary key equality stays byte-exact
- [ ] 1.3 Add the merge itself as a pure function -- current collection plus delta yields the resulting collection -- and check `MAX_TAGS_PER_NODE` / `MAX_METADATA_PAIRS` against that result, not against the request. The function is pure because the service calls it with a collection read under the lock taken in 3.1; nothing about the bound is enforceable without that lock
- [ ] 1.4 Reuse the existing constants; add none. Confirm no new `AuditAction` is introduced, so `ACTIVITY_ACTIONS` and the derived `SECURITY_ACTIONS` are untouched

## 2. Persistence

- [ ] 2.1 Add `add_tags` and `remove_tags` to the node repository port and the SQL adapter: insert with `ON CONFLICT DO NOTHING` on `(node_id, tag)`, delete by `tag IN (…)`
- [ ] 2.2 Add `set_metadata` and `remove_metadata_keys`: upsert on `(node_id, key)`, delete by `key IN (…)`
- [ ] 2.3 Add **no** repository method for the revision. The patch bumps with `node.touch(now)` and persists through the existing `update(node)`, which is safe because the node is read under the lock. Do not express the bump as a SQL increment: `node.etag` is computed from the in-memory `Node`, so a bulk `UPDATE` would leave the aggregate -- and therefore the ETag on the response -- on the pre-patch revision
- [ ] 2.4 Change `replace_metadata` to delete only non-reserved keys before inserting, so a replace leaves the reserved namespace alone. The preservation predicate casefolds the prefix, matching 1.2 and `validate_metadata`
- [ ] 2.5 Mirror the four new methods from 2.1 and 2.2 in `tests/unit/fakes.py`. Record at the fake that it models no unique constraint and no isolation, so `ON CONFLICT` behaviour and every concurrency guarantee are NOT provable here
- [ ] 2.6 Confirm no migration is needed: the unique constraints `ON CONFLICT` requires and the `node_id` cascade already exist, and no constraint or trigger is added for the per-node maxima (see design.md for why the limit is not enforced in the database)

## 3. Use cases

- [ ] 3.1 `patch_tags` in `application/nodes.py`: take `await uow.lock_subtree(node_id)` as the **first** statement, before the node is read, so the node row, the collection, and the revision all come from inside the critical section; then authorize `EDITOR`, check `If-Match`, validate the delta, merge, check the limit, and apply
- [ ] 3.2 `patch_metadata`: the same shape, same lock, same ordering
- [ ] 3.3 Set no isolation level on the session or the engine. The lock orders the two transactions; Postgres' default `READ COMMITTED` is what makes the waiter then *read* what the transaction ahead of it committed, and `REPEATABLE READ` would defeat the limit check while leaving the lock in place
- [ ] 3.4 Short-circuit the no-op: when the merged collection equals the current one -- compared under the lock, so the comparison cannot be invalidated before the response -- skip the row writes, the revision bump, the audit record, and the cache invalidation, and return the current state and its unchanged ETag
- [ ] 3.5 On a real change, `node.touch(now)` then `uow.nodes.update(node)` before `_view`, so the response's ETag is the post-patch one. Do not also issue a SQL revision increment; the two together would bump twice
- [ ] 3.6 On a real change, emit `NODE_TAGS_CHANGED` / `NODE_METADATA_CHANGED` with counts of added and removed entries in the context, and no tag or key text
- [ ] 3.7 Invalidate the same cached listings and node views the replace path invalidates, on a real change only
- [ ] 3.8 Filter the reserved prefix out of the metadata `labels_for` returns, casefolded as in 1.2, so no response shows a reserved pair and the metadata a caller receives is exactly what it may `PUT` back. Leave `uow.nodes.metadata_for` unfiltered -- that is how CyberFS and backup read the namespace
- [ ] 3.9 Add no trashed-node handling: `_authorize` already raises `NotFoundError` when `node.is_deleted`, so a patch is a `404` like `rename` and `move`. Pin it with a test rather than with code

## 4. API

- [ ] 4.1 `PATCH /api/v1/nodes/{node_id}/tags` with an `add`/`remove` body, `extra="forbid"`, each list bounded by `MAX_TAGS_PER_NODE`
- [ ] 4.2 `PATCH /api/v1/nodes/{node_id}/metadata` with a `set` pair list and a `remove` key list, each bounded by `MAX_METADATA_PAIRS`
- [ ] 4.3 Both return `NodeDetail` with the resulting labels and set the `ETag` header, as the `PUT` routes do
- [ ] 4.4 Both accept `If-Match` through the existing dependency
- [ ] 4.5 Confirm the two routes and both bodies appear in the OpenAPI schema, and that the `PUT` routes' request and response shapes are unchanged in it

## 5. Unit tests (fakes, no I/O -- semantics of the merge and the authorization)

- [ ] 5.1 A tag delta adds and removes in one call, and the result is previous ∪ added ∖ removed
- [ ] 5.2 A metadata delta sets named keys and deletes named keys, leaving unnamed keys byte-identical
- [ ] 5.3 A no-op delta leaves the revision unchanged, emits no audit record, and returns the ETag the node already had
- [ ] 5.4 A delta that does change labels bumps the revision and emits the existing action, with counts and no label text in the context
- [ ] 5.5 A stale `If-Match` is `412` even when the delta is a no-op, and nothing changes
- [ ] 5.6 A tag named in both `add` and `remove` is refused; likewise a metadata key in both `set` and `remove`
- [ ] 5.7 An empty delta is refused
- [ ] 5.8 A delta exceeding `MAX_TAGS_PER_NODE` or `MAX_METADATA_PAIRS` *after* the merge is refused and changes nothing -- including the case where the request itself is small and the node is already near the limit
- [ ] 5.9 A removal written in a different case removes the stored tag
- [ ] 5.10 A reserved-prefix key is refused in `set` and in `remove`, including written in mixed case (`CyberFS.trusted`) in each position
- [ ] 5.11 A reserved pair written straight into the fake repository is absent from what `labels_for` returns, while `uow.nodes.metadata_for` still shows it
- [ ] 5.12 Both patches call `uow.lock_subtree` for the node, and do so before reading it -- the shape `tests/unit/test_node_service.py:443` already uses for `move`. The fake's lock is a no-op, so this pins the call, never the serialization
- [ ] 5.13 A `VIEWER` cannot patch either collection; an `EDITOR` on a shared node can
- [ ] 5.14 A patch on a trashed node raises `NotFoundError`, pinning 3.9
- [ ] 5.15 Removing a tag the node does not carry, or a key it does not have, is a success that changes nothing
- [ ] 5.16 The ETag on a successful patch's response equals the one a following `get` returns

## 6. Integration tests (real Postgres/Redis/MinIO -- everything that depends on a constraint, on real concurrency, or on a real cache)

- [ ] 6.1 Patch tags through the API and read them back on `NodeDetail`
- [ ] 6.2 Patch metadata through the API and read it back
- [ ] 6.3 **Concurrency:** two overlapping transactions each add a different tag; both survive. Cannot be a unit test -- `FakeUnitOfWork` models no unique constraint, no advisory lock, and no isolation, so `ON CONFLICT DO NOTHING`, the lock, and the lost-update behaviour only exist against Postgres
- [ ] 6.4 **Concurrency:** two concurrent label-changing patches produce two distinct revisions, proving the lock plus the bump-under-lock rather than two writers persisting `N + 1`
- [ ] 6.5 **Concurrency:** with the node one tag below `MAX_TAGS_PER_NODE`, two concurrent patches each adding two different tags leave the node at the maximum with one patch refused -- the limit guarantee the lock exists for, and the only proof that the pre-read is authoritative
- [ ] 6.6 Adding a tag that already exists does not raise a unique-constraint violation -- the `ON CONFLICT` path, again only real against Postgres
- [ ] 6.7 A patched tag is findable by the existing tag search, and a removed one is not
- [ ] 6.8 A `PUT` after a `PATCH` replaces the whole set, including tags the patch added
- [ ] 6.9 A metadata pair written directly into the reserved namespace at the repository survives a `PUT` of an empty metadata collection, is absent from the `GET` and `PUT` responses, and is refused when a `PATCH` names it as a removal. This is the constraint-adjacent behaviour the fake cannot show, since the fake's replace is a dict assignment
- [ ] 6.10 **Cache:** warm the cached node view and its parent's listing, issue a no-op patch, and assert both entries survive; then issue a real patch and assert both are gone before the response. The only tier where the "invalidates nothing" guarantee is more than an assertion about a fake
- [ ] 6.11 The `ETag` header a patch returns is accepted as the `If-Match` of the next patch, and a `GET` in between reports the same value
- [ ] 6.12 Purging a node still removes its tags and metadata rows after a patch -- the `node_id` cascade, re-confirmed against real Postgres because the fake models no foreign key
- [ ] 6.13 A patch on a node the caller only has `VIEWER` on is refused end to end through the API, not just at the service

## 7. End-to-end tests (live deployment)

- [ ] 7.1 Against the deployment: upload a file, add a tag by `PATCH`, find it by tag, remove the tag by `PATCH`, confirm it is no longer found, and purge
- [ ] 7.2 Against the deployment: `PATCH` metadata to set one key, `PATCH` again to set a second without naming the first, and confirm both are present -- the round-trip-free contribution this change exists for
- [ ] 7.3 Against the deployment: a repeated identical `PATCH` returns the same ETag it returned the first time, and that ETag is accepted as `If-Match`

## 8. Verification and documentation

- [ ] 8.1 `just lint`, `just typecheck`, `just test-unit` clean
- [ ] 8.2 `just test-integration` clean, verified from the CI run rather than assumed, quoting the run and the pass count so the new tests are shown to have executed
- [ ] 8.3 `just test-e2e` clean against the deployment
- [ ] 8.4 Document the two `PATCH` methods in `README.md`: what merges, that a no-op is free, that patches to one node serialize while disjoint patches still both land, and that a replace still wins outright
- [ ] 8.5 Correct the two `PUT` route docstrings in `routers/nodes.py`, which are the OpenAPI descriptions: "Replaces every pair; an empty list clears them" is no longer true of a reserved pair, and the response omits reserved pairs
- [ ] 8.6 Run `openspec validate add-partial-label-updates --strict`
