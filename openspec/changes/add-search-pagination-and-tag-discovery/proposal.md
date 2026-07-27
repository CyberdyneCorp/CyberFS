## Why

Search cannot paginate. `GET /api/v1/search` answers with `SearchResults`, which
carries `items` and nothing else, while `NodePage` — what folder listing
answers with — carries `items` **and** `next_cursor`. A search that matches more
nodes than `limit` therefore returns the first `limit` and drops the rest on the
floor with no signal that it did so and no way to ask for more.

The specification currently blesses this. `file-storage/spec.md`, "Searching by
tag and metadata", ends with:

> **Scenario: Results stay bounded** — WHEN a search matches more nodes than the
> permitted page size, THEN the system SHALL return at most that many rather
> than scanning without limit.

That scenario passes, but only because the remainder is unreachable. Bounding a
page is right; making the rest of the result set inaccessible is not the same
thing, and the spec does not currently distinguish them.

The ordering is a second, quieter defect that pagination exposes. Search orders
by `normalized_name` alone. Names are unique only among siblings, so a search
across a subtree routinely matches several nodes with the same name — every
`notes.md` in every folder. Under a bounded query that only makes the truncation
arbitrary; under a cursor it is a correctness bug, because a cursor built on an
ambiguous sort key can skip or repeat rows.

Third, tags are write-only in practice. A caller can set them and filter on
them, but cannot ask which tags they have. Nothing in the API answers "what is
my vocabulary", so a user must remember the labels they chose, and a UI cannot
offer them. A tagging feature nobody can enumerate decays into a tagging feature
nobody uses.

## What Changes

- **Search paginates**, using the keyset cursor `list_children` already uses.
  `GET /api/v1/search` answers with `NodePage` instead of `SearchResults`, so
  `next_cursor` appears and every match becomes reachable.
- **The order becomes total and is stated as a contract**: normalized name
  ascending, ties broken by node identifier. That tie-break is what makes the
  cursor sound. Callers get a written guarantee they can rely on — and an
  explicit statement that the order is *not* folders-before-files and *not*
  relevance-ranked.
- **A cursor is bound to the filters it was issued for.** Presenting a search
  cursor alongside a different filter set is refused with `422` rather than
  served as if it described that walk.
- **Tag discovery.** `GET /api/v1/tags` lists the tags in use across the nodes
  the caller may search, each with the number of such nodes carrying it,
  ordered by tag, paginated, and optionally narrowed by prefix so a UI can
  offer type-ahead.
- **An explicit any-of mode for tags.** `tag_match=all|any`, defaulting to
  `all`, so today's behaviour is unchanged. The mode governs only how tags
  combine with each other; name and metadata filters keep ANDing.

Not changing: what search matches, who can see what, or the invariant that
**file content is never indexed or matched**. There is no new table, no new
index, and no migration — the ordering the cursor needs is already served by
`ix_nodes_owner_name`, and the tag inventory by `ix_node_tags_tag`.

## Capabilities

### New Capabilities

None. This finishes an existing `file-storage` requirement and adds two beside
it.

### Modified Capabilities

- `file-storage`: "Searching by tag and metadata" replaces the
  "Results stay bounded" scenario with a bound that has a way out of it, and
  gains the any-of tag mode next to the existing all-of semantics where a
  reader will look for it. Two requirements are added: "Paginated and ordered
  search results" and "Tag discovery".

No other capability is touched. In particular `caching/spec.md` is deliberately
left alone: the tag inventory is not cached (see design.md).

## Impact

**Affected code:**

- `src/cyberfs/adapters/outbound/db/repositories.py` — `SqlNodeRepository.search`
  returns a `Page[Node]`, gains the `(normalized_name, id)` order and the
  matching cursor predicate, and gains a tag-inventory query. The existing
  `_paginate` and `encode_cursor`/`decode_cursor` helpers do the work; the only
  new piece is the filter fingerprint carried inside the cursor.
- `src/cyberfs/domain/ports/repositories.py` — the `NodeRepository.search`
  signature returns `Page[Node]` and takes a `cursor`; a `tag_counts` method is
  added.
- `src/cyberfs/application/nodes.py` — the search use case threads the cursor
  through and validates it against the filters; a tag-inventory use case is
  added.
- `src/cyberfs/adapters/inbound/api/routers/nodes.py` and `schemas.py` — the
  search route answers with `NodePage`, gains `cursor` and `tag_match`; a
  `GET /api/v1/tags` route and a `TagPage` schema are added.
- `tests/unit/fakes.py` — the fake node repository must order and paginate the
  same way, or unit tests will pass while the SQL is wrong.

**No schema change.** Nothing is created, dropped, or backfilled, so there is
nothing to roll back beyond the code.

**`SearchResults` survives, and keeps its only remaining caller.**
`GET /api/v1/shared-with-me` also answers with `SearchResults`, and it does not
paginate. Adding `next_cursor` to the shared schema would have promised
pagination on a route that has none. Whether *that* route should paginate is a
real question and a separate one.

**Wire compatibility.** A client reading `items` sees no change: `NodePage` and
`SearchResults` both spell it `items`, and every new field and parameter is
optional. The only behavioural difference for an existing client is that a
truncated result now says so.

**The scope is untouched, so nothing new becomes reachable.** Search still
returns only nodes the caller owns or holds an ACTIVE grant on, still excludes
trashed nodes, and a pending grant still confers nothing. Pagination lets a
caller reach the rest of what was always theirs to reach, in fewer requests
than walking the tree — not more than they could reach before.

**No new `AuditAction`.** Search and the tag inventory are reads of metadata the
caller may already read, and `SECURITY_ACTIONS` is derived as the complement of
`ACTIVITY_ACTIONS`, so a new action would default to a permanently retained
security record. A retained record per keystroke of a type-ahead is the wrong
answer to a question nobody asked.

**No new configuration and no new limits constant.** The page bound is the
existing `PAGE_SIZE_MAX`; the bound on how many tags one query may name is the
existing `MAX_TAGS_PER_NODE`.
