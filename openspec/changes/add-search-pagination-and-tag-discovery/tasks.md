## 1. Domain and ports

- [ ] 1.1 Add a filter-set value object in `src/cyberfs/domain/nodes.py` holding the normalized search filters (term, tag set, tag match mode, metadata key, metadata value) with a stable fingerprint derived from them, so the cursor and the request can be compared without the repository knowing about HTTP
- [ ] 1.2 Add the tag match mode as a `StrEnum` (`all`, `any`) with `all` as the default, and reject any other spelling with `ValidationError`
- [ ] 1.3 Bound the number of tag filters with the existing `MAX_TAGS_PER_NODE`; add no new constant
- [ ] 1.4 Change `NodeRepository.search` in `src/cyberfs/domain/ports/repositories.py` to take a `cursor: str | None` and return `Page[Node]`
- [ ] 1.5 Add `NodeRepository.tag_counts(subject, *, prefix, limit, cursor) -> Page[TagUsage]` and the `TagUsage` value object (tag, count)
- [ ] 1.6 Confirm no new `AuditAction` is introduced, and therefore that `ACTIVITY_ACTIONS` and the derived `SECURITY_ACTIONS` are untouched — a read of metadata is not an auditable event here (design.md)

## 2. Persistence

- [ ] 2.1 Change `SqlNodeRepository.search`'s `ORDER BY` to `(normalized_name, id)` and route the result through the existing `_paginate` helper with a `limit + 1` sentinel
- [ ] 2.2 Add a search cursor predicate mirroring that order, modelled on `_child_cursor_predicate`, and encode the filter fingerprint into the cursor payload alongside the sort key
- [ ] 2.3 Make `decode_cursor` failure and a fingerprint mismatch both raise `ValidationError`, so both surface as `422` through the existing error mapping
- [ ] 2.4 Implement the `any` tag mode as a single `EXISTS` with `tag IN (...)`, keeping the `all` mode as one `EXISTS` per tag so that carrying one tag repeatedly still cannot satisfy "carries all of these"
- [ ] 2.5 Implement `tag_counts`: join `node_tags` to the scoped, non-trashed node set, `GROUP BY tag`, `ORDER BY tag`, anchored `LIKE` for the prefix, `limit + 1` sentinel and cursor on the tag
- [ ] 2.6 Confirm no migration is needed: `ix_nodes_owner_name` serves the ordered scan, `ix_node_tags_tag` serves the filter and the aggregate, `uq_node_tags_node_tag` serves the join back to nodes. If an `EXPLAIN` on a realistic corpus says otherwise, propose the index separately rather than smuggling a migration in here
- [ ] 2.7 Check the query plan for a paginated tag search and for the inventory on a seeded corpus, and record what was observed

## 3. Use cases

- [ ] 3.1 Thread `cursor` and the tag match mode through `NodesService.search`, keeping the existing "at least one filter is required" rule and the `PAGE_SIZE_MAX` clamp
- [ ] 3.2 Build the filter-set object once in the use case and pass it to the repository, so the fingerprint the cursor carries and the fingerprint the request implies are computed by the same code path
- [ ] 3.3 Add a `tag_inventory` use case: authenticate only, no per-node authorization, scope resolved in the query
- [ ] 3.4 Confirm neither path writes to the cache and neither needs invalidation, and that `caching/spec.md`'s enumerated dataset list therefore stays correct as written (design.md)

## 4. API

- [ ] 4.1 Change `GET /api/v1/search` to answer with `NodePage`, and add `cursor` and `tag_match` query parameters
- [ ] 4.2 Leave `SearchResults` in place for `GET /api/v1/shared-with-me`, and confirm that route's response is unchanged
- [ ] 4.3 Add `GET /api/v1/tags` with `prefix`, `limit`, and `cursor`, answering with a `TagPage` of `{tag, count}` plus `next_cursor`
- [ ] 4.4 Confirm no digest appears on either surface — search results carry `NodeSummary`, which has none, and the inventory carries no node identity at all
- [ ] 4.5 Confirm the changed response model and the new route appear correctly in the OpenAPI schema

## 5. Unit tests (fakes, no I/O — `tests/unit`)

These pin the use-case-level rules. They cannot establish the access scope or the
SQL ordering; see section 6 for why.

- [ ] 5.1 Extend the fake node repository to order by `(normalized_name, id)`, honour a cursor, and return `Page[Node]`, so a use-case test exercising pagination is not testing a fake that ignores it
- [ ] 5.2 A cursor presented with a different term, a different tag set, a different tag mode, or a different metadata filter is refused with a validation error and returns nothing
- [ ] 5.3 A malformed or truncated cursor is refused rather than treated as absent
- [ ] 5.4 A limit above `PAGE_SIZE_MAX` is clamped, and the clamped page still reports a cursor when more matches exist
- [ ] 5.5 A search with a cursor but no filter is still refused, so pagination does not become a way to page through everything reachable
- [ ] 5.6 More tag filters than `MAX_TAGS_PER_NODE` is refused in both modes
- [ ] 5.7 `tag_match=any` returns a node carrying one of the tags; `tag_match=all` does not; a node carrying several appears once in `any` mode
- [ ] 5.8 An unrecognized `tag_match` value is refused rather than silently defaulting
- [ ] 5.9 The tag inventory reports normalized tags with counts, ordered by tag, with a cursor when the limit is exceeded, and never reports a zero count
- [ ] 5.10 The inventory `prefix` matches the normalized form: a prefix supplied with different case or surrounding whitespace returns the same tags

## 6. Integration tests (real Postgres/Redis/MinIO, marked `integration`, run in CI — `tests/integration`)

Everything security-relevant lives here. `FakeUnitOfWork` models no foreign keys
and the fake node repository has no view of grants, so the access scope cannot be
established against a fake; and the cursor predicate is evaluated in SQL under
the database collation, so exhaustiveness cannot be established against Python
string ordering.

- [ ] 6.1 Walking every page of a name search returns each match exactly once and misses none, verified against a corpus larger than the page size
- [ ] 6.2 The same, with several matches sharing a name across different parents — the case the missing `id` tie-break breaks
- [ ] 6.3 The same for a tag search and for a metadata search
- [ ] 6.4 The final page carries no cursor
- [ ] 6.5 Results are ordered by normalized name with ties broken by identifier, and the cursor predicate agrees with that `ORDER BY` in the real database collation
- [ ] 6.6 Folders are not grouped before files in search results, unlike `list_children` — pinned so a later "consistency" change has to argue with a test
- [ ] 6.7 A cursor from one filter set presented with another is refused with `422`
- [ ] 6.8 Across a full page walk, another user's unshared node never appears; a node under an ACTIVE grant does; a node under a PENDING grant does not; a trashed node does not
- [ ] 6.9 `tag_match=any` across a corpus spanning multiple pages returns the union with no duplicates and no omissions
- [ ] 6.10 The tag inventory counts only nodes in the caller's scope: two users with different access to the same tagged nodes see different counts for the same tag
- [ ] 6.11 The inventory agrees with search — a tag reported with count `n` yields exactly `n` nodes across a full paginated walk of that single-tag search
- [ ] 6.12 Trashing the last node carrying a tag removes the tag from the inventory rather than reporting it with a count of zero
- [ ] 6.13 Purging the last node carrying a tag does the same, which additionally exercises the `node_tags` FK cascade — provable only here, because the fake models no foreign keys
- [ ] 6.14 The inventory paginates: a vocabulary larger than the limit is walked exactly once through, and the `prefix` filter narrows it
- [ ] 6.15 An inventory cursor presented with a different prefix is refused with `422`
- [ ] 6.16 `GET /api/v1/tags` without credentials returns `401` and no tag
- [ ] 6.17 `GET /api/v1/shared-with-me` still returns the `SearchResults` shape, with no `next_cursor` field appearing

## 7. End-to-end tests (against a live deployment, marked `e2e` — `tests/e2e`)

- [ ] 7.1 Against the deployment: create more nodes than one page, search, follow `next_cursor` to exhaustion, assert the set of identifiers collected equals the set created, then clean up by purging
- [ ] 7.2 Against the deployment: tag those nodes, read `GET /api/v1/tags`, and assert the reported count matches what the paginated tag search returns

## 8. Verification and documentation

- [ ] 8.1 `just lint`, `just typecheck`, `just test-unit` clean
- [ ] 8.2 `just test-integration` clean, verified in CI rather than assumed, quoting the run and the test count so the new tests are visibly executed
- [ ] 8.3 `just test-e2e` clean against the deployment
- [ ] 8.4 Update `docs/api.md`: the `/api/v1/search` row gains `cursor` and `tag_match`, and a `/api/v1/tags` row is added
- [ ] 8.5 Update `README.md` where it describes metadata search, stating that search paginates and that the tag inventory is scoped per caller so its counts are not global
- [ ] 8.6 Run `openspec validate add-search-pagination-and-tag-discovery --strict`
