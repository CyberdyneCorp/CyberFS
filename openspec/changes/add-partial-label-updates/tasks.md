## 1. Domain

- [ ] 1.1 Add a tag-delta validator in `src/cyberfs/domain/nodes.py`: normalize both the additions and the removals with `normalize_tag`, refuse blank and over-length entries as `validate_tags` does, and refuse a tag named in both directions
- [ ] 1.2 Add a metadata-delta validator: key and value length limits, no duplicate key within the request, no reserved-prefix key in either the set list or the removal list, and no key named in both directions
- [ ] 1.3 Add the merge itself as a pure function -- current collection plus delta yields the resulting collection -- and check `MAX_TAGS_PER_NODE` / `MAX_METADATA_PAIRS` against that result, not against the request
- [ ] 1.4 Reuse the existing constants; add none. Confirm no new `AuditAction` is introduced, so `ACTIVITY_ACTIONS` and the derived `SECURITY_ACTIONS` are untouched

## 2. Persistence

- [ ] 2.1 Add `add_tags` and `remove_tags` to the node repository port and the SQL adapter: insert with `ON CONFLICT DO NOTHING` on `(node_id, tag)`, delete by `tag IN (…)`
- [ ] 2.2 Add `set_metadata` and `remove_metadata_keys`: upsert on `(node_id, key)`, delete by `key IN (…)`
- [ ] 2.3 Add a revision bump expressed as a SQL increment (`revision = revision + 1`, `updated_at = :now`) in the pattern the recursive soft delete already uses, and return the resulting revision so the response carries a correct ETag
- [ ] 2.4 Change `replace_metadata` to delete only non-reserved keys before inserting, so a replace leaves the reserved namespace alone
- [ ] 2.5 Mirror all four new methods in `tests/unit/fakes.py`. Record at the fake that it models no unique constraint, so `ON CONFLICT` behaviour and the concurrency guarantees are NOT provable here
- [ ] 2.6 Confirm no migration is needed: the unique constraints `ON CONFLICT` requires and the `node_id` cascade already exist

## 3. Use cases

- [ ] 3.1 `patch_tags` in `application/nodes.py`: authorize `EDITOR`, check `If-Match` **before** computing the effect, validate the delta, merge, and apply
- [ ] 3.2 `patch_metadata`: the same shape
- [ ] 3.3 Short-circuit the no-op: when the merged collection equals the current one, skip the row writes, the revision bump, the audit record, and the cache invalidation, and return the current state
- [ ] 3.4 On a real change, emit `NODE_TAGS_CHANGED` / `NODE_METADATA_CHANGED` with counts of added and removed entries in the context, and no tag or key text
- [ ] 3.5 Invalidate the same cached listings and node views the replace path invalidates, on a real change only
- [ ] 3.6 Settle design.md's open question -- whether a patch on a trashed node is refused -- against what the authorization path actually does, and pin it with a test

## 4. API

- [ ] 4.1 `PATCH /api/v1/nodes/{node_id}/tags` with an `add`/`remove` body, `extra="forbid"`, each list bounded by `MAX_TAGS_PER_NODE`
- [ ] 4.2 `PATCH /api/v1/nodes/{node_id}/metadata` with a `set` pair list and a `remove` key list, each bounded by `MAX_METADATA_PAIRS`
- [ ] 4.3 Both return `NodeDetail` with the resulting labels and set the `ETag` header, as the `PUT` routes do
- [ ] 4.4 Both accept `If-Match` through the existing dependency
- [ ] 4.5 Confirm the two routes and both bodies appear in the OpenAPI schema, and that the `PUT` routes are unchanged in it

## 5. Unit tests (fakes, no I/O -- semantics of the merge and the authorization)

- [ ] 5.1 A tag delta adds and removes in one call, and the result is previous ∪ added ∖ removed
- [ ] 5.2 A metadata delta sets named keys and deletes named keys, leaving unnamed keys byte-identical
- [ ] 5.3 A no-op delta leaves the revision unchanged and emits no audit record
- [ ] 5.4 A delta that does change labels bumps the revision and emits the existing action, with counts and no label text in the context
- [ ] 5.5 A stale `If-Match` is `412` even when the delta is a no-op, and nothing changes
- [ ] 5.6 A tag named in both `add` and `remove` is refused; likewise a metadata key in both `set` and `remove`
- [ ] 5.7 An empty delta is refused
- [ ] 5.8 A delta exceeding `MAX_TAGS_PER_NODE` or `MAX_METADATA_PAIRS` *after* the merge is refused and changes nothing -- including the case where the request itself is small and the node is already near the limit
- [ ] 5.9 A removal written in a different case removes the stored tag
- [ ] 5.10 A reserved-prefix key is refused in `set` and in `remove`
- [ ] 5.11 A `VIEWER` cannot patch either collection; an `EDITOR` on a shared node can
- [ ] 5.12 Removing a tag the node does not carry, or a key it does not have, is a success that changes nothing

## 6. Integration tests (real Postgres/Redis/MinIO -- everything that depends on a constraint or on real concurrency)

- [ ] 6.1 Patch tags through the API and read them back on `NodeDetail`
- [ ] 6.2 Patch metadata through the API and read it back
- [ ] 6.3 **Concurrency:** two overlapping transactions each add a different tag; both survive. Cannot be a unit test -- `FakeUnitOfWork` models no unique constraint and no isolation, so `ON CONFLICT DO NOTHING` and the lost-update behaviour only exist against Postgres
- [ ] 6.4 **Concurrency:** two concurrent label-changing patches produce two distinct revisions, proving the SQL increment rather than the Python read-modify-write
- [ ] 6.5 Adding a tag that already exists does not raise a unique-constraint violation -- the `ON CONFLICT` path, again only real against Postgres
- [ ] 6.6 A patched tag is findable by the existing tag search, and a removed one is not
- [ ] 6.7 A `PUT` after a `PATCH` replaces the whole set, including tags the patch added
- [ ] 6.8 A metadata pair written directly into the reserved namespace at the repository survives a `PUT` of an empty metadata collection, and a `PATCH` naming it as a removal is refused. This is the FK/constraint-adjacent behaviour the fake cannot show, since the fake's replace is a dict assignment
- [ ] 6.9 Purging a node still removes its tags and metadata rows after a patch -- the `node_id` cascade, re-confirmed against real Postgres because the fake models no foreign key
- [ ] 6.10 A patch on a node the caller only has `VIEWER` on is refused end to end through the API, not just at the service

## 7. End-to-end tests (live deployment)

- [ ] 7.1 Against the deployment: upload a file, add a tag by `PATCH`, find it by tag, remove the tag by `PATCH`, confirm it is no longer found, and purge
- [ ] 7.2 Against the deployment: `PATCH` metadata to set one key, `PATCH` again to set a second without naming the first, and confirm both are present -- the round-trip-free contribution this change exists for
- [ ] 7.3 Against the deployment: a repeated identical `PATCH` returns the same ETag it returned the first time

## 8. Verification and documentation

- [ ] 8.1 `just lint`, `just typecheck`, `just test-unit` clean
- [ ] 8.2 `just test-integration` clean, verified from the CI run rather than assumed, quoting the run and the pass count so the new tests are shown to have executed
- [ ] 8.3 `just test-e2e` clean against the deployment
- [ ] 8.4 Document the two `PATCH` methods in `README.md`: what merges, that a no-op is free, that concurrent patches on disjoint labels both land, and that a replace still wins outright
- [ ] 8.5 Run `openspec validate add-partial-label-updates --strict`
