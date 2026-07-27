## 1. Domain and ports

- [ ] 1.1 Move `encode_cursor`/`decode_cursor` out of `src/cyberfs/adapters/outbound/db/repositories.py` into a `src/cyberfs/domain/pagination.py` beside `Page`, keeping the base64/`\x1f` payload format byte-identical and keeping `ValidationError` as the failure, and re-export both names from `repositories.py` so `activity_queries.py`, the audit feed, the admin listings, and the trash listing being built in parallel need no edit
- [ ] 1.2 Add a keyed cursor to that module — `encode_keyed_cursor(fingerprint, key_parts)` and `decode_keyed_cursor(cursor, fingerprint) -> tuple[str, ...]`, the latter raising `ValidationError` both for an undecodable payload and for a fingerprint that does not match the one supplied — written generically so any listing whose scope lives in the query string can bind its cursor the same way
- [ ] 1.3 Add a search filter-set value object in `src/cyberfs/domain/nodes.py` holding the normalized filters (term, tag set, tag match mode, metadata key, metadata value) and exposing the stable fingerprint derived from them, so the cursor and the request are compared without the repository or the router knowing how
- [ ] 1.4 Add the tag match mode as a `StrEnum` (`all`, `any`) with `all` as the default, and reject any other spelling with `ValidationError`
- [ ] 1.5 Bound the number of tag filters with the existing `MAX_TAGS_PER_NODE`; add no new constant
- [ ] 1.6 Change `NodeRepository.search` in `src/cyberfs/domain/ports/repositories.py` to take the filter-set object and an already-decoded `after: tuple[str, str] | None` sort key — never a raw cursor — and to return `Page[Node]`
- [ ] 1.7 Add `NodeRepository.tag_counts(subject, *, prefix, limit, after)` returning `Page[TagUsage]`, plus the `TagUsage` value object (tag, count), with `after` likewise an already-decoded tag rather than a cursor
- [ ] 1.8 Confirm no new `AuditAction` is introduced, and therefore that `ACTIVITY_ACTIONS` and the derived `SECURITY_ACTIONS` are untouched — a read of metadata is not an auditable event here (design.md)

## 2. Persistence

- [ ] 2.1 Extract the scope predicate `SqlNodeRepository.search` builds today — owned or actively granted, and not trashed — into one private helper, and use it from `search` and from `tag_counts` so the two cannot drift; the helper keeps today's semantics exactly (direct grant rows, no ancestor walk)
- [ ] 2.2 Change `SqlNodeRepository.search`'s `ORDER BY` to `(normalized_name, id)` and route the result through the existing `_paginate` helper with a `limit + 1` sentinel
- [ ] 2.3 Add a search cursor predicate mirroring that order, modelled on `_child_cursor_predicate`, built from the decoded `after` key the use case passes in — the adapter turns a key into a `WHERE`, and contains no cursor parsing and no fingerprint logic
- [ ] 2.4 Derive each next cursor with `encode_keyed_cursor`, passing the fingerprint carried by the filter-set object the use case handed over, so the cursor a page hands back is bound to the filters that produced it
- [ ] 2.5 Implement the `any` tag mode as a single `EXISTS` with `tag IN (...)`, keeping the `all` mode as one `EXISTS` per tag so that carrying one tag repeatedly still cannot satisfy "carries all of these"
- [ ] 2.6 Implement `tag_counts`: join `node_tags` to the node set the 2.1 helper scopes, `GROUP BY tag`, `ORDER BY tag`, `limit + 1` sentinel, and the keyed cursor on the tag
- [ ] 2.7 Normalize the inventory `prefix` with `normalize_tag` and escape it with the existing `_escape_like` before the anchored `LIKE`, so a caller-supplied `%` or `_` matches literally rather than widening the aggregate — `normalize_tag` only folds case and strips, it does not touch pattern characters
- [ ] 2.8 Confirm no migration is needed: `ix_nodes_owner_name` serves the ordered scan, `ix_node_tags_tag` serves the filter and the aggregate, `uq_node_tags_node_tag` serves the join back to nodes. If an `EXPLAIN` on a realistic corpus says otherwise, propose the index separately rather than smuggling a migration in here
- [ ] 2.9 `EXPLAIN` a paginated name search (first page and a cursored page) and the inventory aggregate on a seeded corpus, and record the plans in the change: design.md predicts that the cursor predicate lets a later page start where the last ended and admits both the `OR`-shaped scope predicate and the three-way keyset `OR` may defeat that, and that the aggregate is not index-only because it joins back to `nodes`. Write down what the planner actually chose either way

## 3. Use cases

- [ ] 3.1 Thread `cursor` and the tag match mode through `NodeService.search`, keeping the existing "at least one filter is required" rule and the `min(limit, self._page_size_max)` clamp
- [ ] 3.2 Build the filter-set object once in the use case, and when a cursor is present decode it there with `decode_keyed_cursor` against that object's fingerprint, so a malformed cursor and a filter mismatch both raise `ValidationError` from the layer the fake can exercise; pass the repository the object and the decoded key
- [ ] 3.3 Add a `tag_inventory` use case: authenticate only, no per-node authorization, scope resolved in the query, and the same cursor decode against a fingerprint over the normalized prefix
- [ ] 3.4 Confirm neither path writes to the cache and neither needs invalidation, and that `caching/spec.md`'s enumerated dataset list therefore stays correct as written (design.md)

## 4. API

- [ ] 4.1 Change `GET /api/v1/search` to answer with `NodePage`, and add `cursor` and `tag_match` query parameters
- [ ] 4.2 Leave the route's `limit: Annotated[int, Query(ge=1, le=1000)] = 100` exactly as it is, matching `list_children` and the admin listings: an over-large limit is refused by FastAPI with `422`, and a limit under the ceiling but over a lowered `PAGE_SIZE_MAX` is clamped in the use case. The delta scenario covers the pair; do not derive either bound from settings here (design.md non-goal)
- [ ] 4.3 Leave `SearchResults` in place for `GET /api/v1/shared-with-me`, and confirm that route's response is unchanged
- [ ] 4.4 Add `GET /api/v1/tags` with `prefix`, `limit`, and `cursor`, answering with a `TagPage` of `{tag, count}` plus `next_cursor`
- [ ] 4.5 Confirm no digest appears on either surface — search results carry `NodeSummary`, which has none, and the inventory carries no node identity at all
- [ ] 4.6 Confirm the changed response model and the new route appear correctly in the OpenAPI schema

## 5. Unit tests (fakes, no I/O — `tests/unit`)

These pin the rules that live in the domain and the use case: cursor decoding, the
fingerprint comparison, the clamp, and the filter validation. They cannot
establish the access scope or the SQL ordering; see section 6 for why. Because the
cursor codec and the fingerprint are production code in `domain`, the fake
reimplements neither — its whole job is ordering, slicing after the decoded key,
and returning `Page`.

- [ ] 5.1 Extend the fake node repository to order by `(normalized_name, id)`, slice after the `after` key it is given, and return `Page[Node]` whose `next_cursor` comes from the shared `encode_keyed_cursor`, so a use-case test exercising pagination is not testing a fake that ignores it
- [ ] 5.2 A cursor presented with a different term, a different tag set, a different tag mode, or a different metadata filter is refused with a validation error and returns nothing
- [ ] 5.3 A malformed or truncated cursor is refused rather than treated as absent
- [ ] 5.4 `NodeService.search` called with a limit above the configured `PAGE_SIZE_MAX` clamps to it, and the clamped page still reports a cursor when more matches exist — asserted against the service, because the route's static ceiling refuses such a limit before a use case runs (see 6.18)
- [ ] 5.5 A search with a cursor but no filter is still refused, so pagination does not become a way to page through everything reachable
- [ ] 5.6 More tag filters than `MAX_TAGS_PER_NODE` is refused in both modes
- [ ] 5.7 `tag_match=any` returns a node carrying one of the tags; `tag_match=all` does not; a node carrying several appears once in `any` mode
- [ ] 5.8 An unrecognized `tag_match` value is refused rather than silently defaulting to `all`
- [ ] 5.9 The tag inventory reports normalized tags with counts, ordered by tag, with a cursor when the limit is exceeded, and never reports a zero count
- [ ] 5.10 The inventory `prefix` matches the normalized form: a prefix supplied with different case or surrounding whitespace returns the same tags
- [ ] 5.11 `_escape_like` escapes `%`, `_`, and `\` — a direct test of the helper, which has none today. Whether the inventory *calls* it is not provable here: a Python `startswith` in the fake treats `%` literally whatever the SQL does, so the behavioural proof is 6.17 and this task exists only so the helper itself is covered
- [ ] 5.12 `decode_keyed_cursor` round-trips what `encode_keyed_cursor` produced and rejects the same payload presented with a different fingerprint — the codec's own test, independent of any repository
- [ ] 5.13 `encode_cursor`/`decode_cursor` imported from `adapters.outbound.db.repositories` still resolve after the move, so the re-export is covered rather than assumed

## 6. Integration tests (real Postgres/Redis/MinIO, marked `integration`, run in CI — `tests/integration`)

Everything security-relevant lives here. `FakeUnitOfWork` models no foreign keys
and the fake node repository has no view of grants — `FakeNodeRepository.search`
ignores `subject` entirely — so the access scope cannot be established against a
fake; and the cursor predicate is evaluated in SQL under the database collation,
so exhaustiveness cannot be established against Python string ordering.

- [ ] 6.1 Walking every page of a name search returns each match exactly once and misses none, verified against a corpus larger than the page size
- [ ] 6.2 The same, with several matches sharing a name across different parents — the case the missing `id` tie-break breaks
- [ ] 6.3 The same for a tag search and for a metadata search
- [ ] 6.4 The final page carries no cursor
- [ ] 6.5 Results are ordered by normalized name with ties broken by identifier, and the cursor predicate agrees with that `ORDER BY` in the real database collation
- [ ] 6.6 Folders are not grouped before files in search results, unlike `list_children` — pinned so a later "consistency" change has to argue with a test
- [ ] 6.7 A cursor from one filter set presented with another is refused with `422`
- [ ] 6.8 Across a full page walk, another user's unshared node never appears; a node under an ACTIVE grant does; a node under a PENDING grant does not; a trashed node does not
- [ ] 6.9 A file inside a folder shared with the caller does not appear in that caller's search results even when it satisfies the filter, while the shared folder itself does — and the caller can still `GET` that file, proving search is narrower than read access by design rather than by accident
- [ ] 6.10 `tag_match=any` across a corpus spanning multiple pages returns the union with no duplicates and no omissions
- [ ] 6.11 The tag inventory counts only nodes in the caller's scope: two users with different access to the same tagged nodes see different counts for the same tag
- [ ] 6.12 The inventory agrees with search — a tag reported with count `n` yields exactly `n` nodes across a full paginated walk of that single-tag search — asserted for a recipient whose access comes from a grant as well as for an owner, since owned nodes are the case two different scope predicates would agree on anyway
- [ ] 6.13 A tag carried only by a descendant of a shared folder does not appear in the recipient's inventory, matching 6.9, so the aggregate and the search agree on the shared case too
- [ ] 6.14 Trashing the last node carrying a tag removes the tag from the inventory rather than reporting it with a count of zero
- [ ] 6.15 Purging the last node carrying a tag does the same, which additionally exercises the `node_tags` FK cascade — provable only here, because the fake models no foreign keys
- [ ] 6.16 The inventory paginates: a vocabulary larger than the limit is walked exactly once through, and the `prefix` filter narrows it
- [ ] 6.17 A prefix of `%` and a prefix of `a_` match literally against real tags rather than behaving as wildcards
- [ ] 6.18 `GET /api/v1/search?limit=1001` is refused with `422` and returns no results, pinning the route ceiling that makes the delta's "never served in full" scenario true on a default deployment
- [ ] 6.19 An inventory cursor presented with a different prefix is refused with `422`
- [ ] 6.20 `GET /api/v1/tags` without credentials returns `401` and no tag
- [ ] 6.21 `GET /api/v1/shared-with-me` still returns the `SearchResults` shape, with no `next_cursor` field appearing

## 7. End-to-end tests (against a live deployment, marked `e2e` — `tests/e2e`)

Kept deliberately small: every node here is a real authenticated create plus a
real purge against a live deployment, so exhaustion is proven with an explicit
small `limit` rather than by out-creating the route's default of `100`.

- [ ] 7.1 Create a handful of nodes sharing a name fragment, search with `limit=2`, follow `next_cursor` to exhaustion, assert the final response carries no cursor and that the set of identifiers collected equals the set created, then clean up by purging
- [ ] 7.2 Tag those same nodes, walk `GET /api/v1/tags` with a small `limit` and the tag's prefix, and assert the reported count matches the number of nodes the paginated tag search returns

## 8. Verification and documentation

- [ ] 8.1 `just lint`, `just typecheck`, `just test-unit` clean
- [ ] 8.2 `just test-integration` clean, verified in CI rather than assumed, quoting the run and the test count so the new tests are visibly executed
- [ ] 8.3 `just test-e2e` clean against the deployment
- [ ] 8.4 Update `docs/api.md`: the `/api/v1/search` row gains `cursor` and `tag_match`, and a `/api/v1/tags` row is added
- [ ] 8.5 Update `README.md` where it describes metadata search, stating that search paginates, that a grant makes the granted node findable but not its descendants, and that the tag inventory is scoped per caller so its counts are not global
- [ ] 8.6 Run `openspec validate add-search-pagination-and-tag-discovery --strict`
