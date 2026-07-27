## Context

`SqlNodeRepository.search` builds a list of AND-ed conditions — the
owned-or-actively-granted scope, the trashed exclusion, an `ILIKE` on
`normalized_name`, one `EXISTS` per tag, one `EXISTS` for the metadata pair —
and ends with:

```
select(m.NodeRow).where(*conditions).order_by(m.NodeRow.normalized_name).limit(limit)
```

It returns `tuple[Node, ...]`. There is no cursor anywhere in the path, and
`SearchResults` has no field to put one in.

`list_children`, three screens above it, already does this properly: a
deterministic `ORDER BY` whose last component is `id`, a cursor predicate
(`_child_cursor_predicate`) that mirrors the same tuple comparison, a `limit + 1`
sentinel row, and `_paginate` to trim the sentinel and derive the next cursor.
`encode_cursor`/`decode_cursor` are base64 over a `\x1f`-joined string, and
`decode_cursor` raises `ValidationError`, which `errors.py` maps to `422`.

So the mechanism exists and is exercised in production by folder listing, the
admin user list, the audit feed, and the activity feed. This change reuses it
rather than adding a second one.

## Goals / Non-Goals

**Goals:**

- Make every search result reachable, not just the first page.
- Give the result order a written, total, cursor-safe definition.
- Let a caller enumerate their own tag vocabulary with usage counts.
- Let a caller ask for "any of these tags" without issuing N searches.

**Non-Goals:**

- **Full-text or content search.** Considered and rejected outright, not
  deferred. File content is encrypted and is never indexed or matched; that
  invariant is load-bearing for the whole encryption story, and an index over
  plaintext tokens would reconstruct enough of the content to make the
  encryption ornamental. `file-storage/spec.md` states it as a scenario
  ("Content is not searchable") and this change leaves it exactly as it is.
- **Search by content digest.** Also considered and rejected, not deferred.
  It is the natural next filter and it is a deduplication tool people ask for,
  but it converts the plaintext digest into a lookup key, which is precisely the
  confirmation-attack shape the existing digest handling avoids: the digest is
  given to owners and active share recipients — who can read the bytes and
  compute it anyway — and withheld from every `/api/v1/admin/*` response so no
  operator can test whether a user holds a specific known file. A
  `?digest=` parameter would hand that same test to anyone with an account, one
  request at a time, against every node they can reach. It stays out, and the
  digest stays absent from search results, which carry `NodeSummary` and no
  digest today.
- **A total match count.** `NodePage` has no `total` and this change does not add
  one. An exact count means running the same predicate a second time without
  the limit, on every page, for a number that is stale the moment it is
  computed — and its presence invites clients to build offset paging on top of
  it.
- **Relevance ranking and recency ordering.** See the ordering decision below.
- **Metadata filters beyond today's single key (optionally pinned to a value).**
  Multiple keys, OR over keys, value substrings, and range comparisons are all
  separate, and all want an argument about query-language surface that this
  change does not need to have.
- **Tag rename and merge.** The inventory makes the need visible — you cannot
  merge `draft` and `drafts` until you can see you have both — but rewriting
  labels across a subtree is a bulk mutation with its own authorization,
  auditing, and revision-bump story.
- **An administrative view of a user's tags.** The admin surface has no
  node-level endpoint today; when one arrives it inherits the existing
  unresolved question about ungated operator access to labels, and that argument
  belongs to `admin-dashboard`.
- **Paginating `GET /api/v1/shared-with-me`.** It shares the `SearchResults`
  schema and has the same unbounded-result problem. Fixing it here would smuggle
  a `sharing` change into a `file-storage` one.
- **Replacing the routes' literal `limit` default of `100` with
  `PAGE_SIZE_DEFAULT`.** The same literal appears on `list_children`; changing
  one and not the other trades a small inconsistency for a worse one.

## Decisions

**Reuse the keyset cursor, do not add offset paging.** `?offset=` is less code
and every client understands it, and it is wrong here for two reasons that
compound: the cost of page *n* grows with *n*, because Postgres must produce and
discard the rows before it; and a node created, renamed, or trashed during the
walk shifts every later row, so pages silently duplicate or skip. Keyset paging
has neither problem, and this codebase already has exactly one pagination
mechanism. A second one would be the thing future readers have to hold in their
heads.

**The sort key gains `id`, and that is the real fix.** `ORDER BY
normalized_name` is not a total order over a search result: names are unique
only among siblings (`uq` index is per-parent), and a search spans parents. Two
matches named `notes.md` have no defined relative position, so a cursor holding
only the name cannot say which of them the next page starts after — it will
either re-emit one or drop one. `(normalized_name, id)` is total because `id` is
a primary key, and it mirrors `list_children`'s `(kind_rank, normalized_name,
id)` down to the `\x1f`-joined cursor payload. Without the tie-break,
pagination would be a bug factory that only shows up on corpora large enough to
have duplicate names — which is to say, on real ones.

**What a caller may rely on: name ascending, ties by identifier, nothing else.**
Stated explicitly in the spec, including the two things it is *not*:

- *Not folders-before-files.* `list_children` ranks kind first because a folder
  listing is a place you browse. A search result is a set of answers, and
  pushing every folder to the front of a name-ordered answer list makes the
  ordering harder to predict, not easier.
- *Not relevance.* There is no scoring model, and there should not be one behind
  a cursor: a relevance score computed per query is fine, but a keyset cursor
  needs the sort key to be a stored, comparable, stable column. A score is none
  of those.

The order is the database collation's ascending order of `normalized_name`, and
the spec says so rather than promising a particular alphabet. `ORDER BY` and the
cursor's `>` comparison both evaluate in the database under the same collation,
so they agree with each other; what they must not do is promise a caller that
`Z` sorts before `a`, which depends on how the cluster was initialised.

**Ordering by tag or by recency is deferred, with reasons.** Name order when
filtering by tag is admittedly arbitrary — "everything tagged `invoice`" has no
natural alphabetical meaning, and `updated_at DESC` is what a human actually
wants. It is deferred rather than dismissed because a recency cursor is a
different animal: `updated_at` is mutable, so a node edited mid-walk jumps
between pages, and the cursor needs `(updated_at, id)` plus an explicit
statement that exactly-once no longer holds under concurrent edits. That is a
contract worth writing carefully once there is a client for it, not a parameter
worth bolting on now. What matters for this change is that the order be
*deterministic*; being *useful* is the follow-up.

**Search answers with `NodePage`; `SearchResults` is left to
`/shared-with-me`.** The alternative — add `next_cursor` to `SearchResults` —
is one line and is rejected because both routes share that schema, so
`/shared-with-me` would start advertising a `next_cursor` it never populates.
A field that is structurally always `null` is worse than an absent field: it
tells a client the route paginates. `NodePage` already means "a page of nodes"
and already has the right two fields.

**The cursor carries a fingerprint of the filter set, and a mismatch is
`422`.** A search cursor encodes a position in one specific ordered result set.
Present it with different filters and the position is meaningless — the server
would happily return a page of the *new* filter's results starting after some
name from the *old* filter's walk, which is plausible-looking nonsense: no
error, no duplicate, just a silently missing prefix of the results. That is the
failure mode that produces bug reports nobody can reproduce. So the cursor
carries a short digest of the normalized filter set (term, tags, mode, key,
value) and the server refuses a cursor whose digest does not match the request.

`list_children` needs none of this because its scope is the `parent_id` in the
path, so a cursor cannot be moved to a different listing without changing the
URL. Search's scope is the query string, which is exactly where a client's
pagination loop is most likely to get it wrong.

Rejected alternative: document "resend the same filters" and leave the outcome
undefined. Cheaper, and it makes the specification describe something the
implementation does not actually guarantee.

Consequence accepted: if the fingerprint's computation ever changes, cursors
issued by the previous deployment are refused with `422` during a rollout.
That is the same class of breakage as a cache schema bump, cursors live for
seconds, and the refusal is loud rather than silent.

**`tag_match=all|any`, defaulting to `all`, governing only the tags.** The
existing AND semantics were a deliberate decision and stay the default, so no
existing query changes meaning. The case for finally adding `any` is that
pagination changed the arithmetic. Before, "anything tagged `draft` or `wip`"
was two requests whose results a client could concatenate and dedupe — clumsy
but complete. Now it is two independent cursor walks, and merging them cannot
honour a single page boundary or produce a stable order across the seam without
holding every result in the client. The client-side workaround got strictly
worse at the moment paging arrived, which is the right moment to reconsider it.

The cost is one enum parameter, and in SQL the `any` form is *cheaper* than the
`all` form it joins: one `EXISTS` with `tag IN (...)` instead of one `EXISTS`
per tag.

The mode is scoped to the tag filter alone — name and metadata continue to AND,
and the spec says so — because "does `any` also loosen the name match" is the
ambiguity that would make the parameter a liability. Rejected alternative: a
general boolean query expression (`tag:a AND (tag:b OR key:x=y)`). That is a
parser, a precedence table, an unbounded cost model, and a new injection
surface, in exchange for expressiveness nobody has asked for. Also rejected:
inferring `any` from repeated parameters with a different name (`any_tag=`),
which encodes the mode in the spelling of a parameter and makes the two modes
impossible to reject as mutually exclusive.

**Bound the number of tag filters with `MAX_TAGS_PER_NODE`, and refuse rather
than return nothing.** In `all` mode, naming more tags than a node may carry
can never match, so serving it is a guaranteed-empty scan; in `any` mode the
query is meaningful but the fan-out is what the bound is for. Reusing the
existing constant avoids inventing a limit that would then need its own
justification, and both modes get the same bound so the parameter's validity
does not depend on the mode.

**Tag discovery is its own endpoint returning tags with counts, not a facet
block bolted onto search results.** Facets attached to a search response would
tie "what is my vocabulary" to "what matches this query", make every search pay
for a group-by, and give the response two unrelated pagination stories. A
separate `GET /api/v1/tags` is one indexed aggregate that answers the question
being asked.

**The inventory is scoped exactly like search: owned, or an ACTIVE grant.** Any
other scoping would be a second access model to maintain and reason about. The
invariant this buys is worth stating as a scenario: a tag the inventory reports
with count *n* returns *n* nodes when used as a filter. Counts are therefore
per-caller, not global, and the spec says that too — otherwise someone will
treat the number as a property of the tag and be confused when two users
disagree about it.

**The inventory is ordered by tag ascending, not by count descending.** Count
order is what a "top tags" widget wants, and it is unsafe under a cursor: a
count changes whenever *any* node in reach is labelled, so a tag can move across
the page boundary mid-walk and be skipped or repeated. A tag's own spelling, by
contrast, never changes — a tag row is inserted or deleted, never renamed — so
`(tag)` is both total (it is unique in the aggregate) and stable. A client that
wants a ranking sorts what it fetched; the inventory is bounded by the caller's
own vocabulary, and the `prefix` filter keeps that bounded even for a large one.

**A `prefix` filter on the inventory, matched against the normalized tag
form.** Type-ahead is the whole point of discovery in a UI, and an anchored
`LIKE 'pre%'` is served by the existing `ix_node_tags_tag` — unlike the
unanchored `ILIKE` the name search is stuck with. Matching the normalized form
means case and surrounding whitespace do not matter, consistent with how a tag
filter already matches. The inventory's cursor is bound to its filter set by the
same rule as search's, so `prefix` cannot be changed mid-walk either; one rule,
both endpoints.

**Zero-count tags do not exist.** The aggregate is over live, in-scope nodes, so
a tag whose last carrier is trashed or purged simply stops appearing. There is
no tag entity to leave behind — `node_tags` rows are per-node and cascade on
`node_id` — and inventing one so a count could reach zero would create the
vocabulary-management problem that "tag rename and merge" is a non-goal of.

**The inventory is not cached.** `caching/spec.md` enumerates the cached
datasets *exactly*, so caching this would mean modifying that requirement, and
the invalidation obligation ("every mutation SHALL invalidate the cache entries
it can affect, within the same request") is nasty here: one `PUT /tags` on one
shared node invalidates the inventory of the owner and of every subject holding
a grant anywhere above it. For a single indexed `GROUP BY` over an index-only
scan, that is a large amount of correctness risk bought with a small amount of
latency. Grant listings and audit records are already read straight from
Postgres for comparable reasons.

**No new `AuditAction`.** The `file-storage` requirement "File operations are
auditable" requires a record when content is *downloaded*, which is what lets an
owner establish that their bytes were read; a metadata search reads no content.
And because `SECURITY_ACTIONS` is derived as the complement of
`ACTIVITY_ACTIONS`, a new action added carelessly becomes a permanently retained
security record — a per-keystroke record from a type-ahead is the worst possible
thing to retain forever. If search auditing is ever wanted it should be a
deliberate change that argues for the retention class.

**No migration.** `ix_nodes_owner_name` (`owner_id, normalized_name`) already
serves the ordered scan for the owned branch, which is the dominant one;
`ix_node_tags_tag` serves both the tag filter and the inventory's `GROUP BY` and
`prefix`; `uq_node_tags_node_tag` covers the join back to nodes. The `id`
tie-break only sorts within equal-name groups, which are small. Adding
`id` to the index is the obvious tuning knob and is deliberately not taken
without a measurement to justify it.

## Risks / Trade-offs

- **The fake repository can make pagination look correct while the SQL is
  wrong.** `FakeUnitOfWork` sorts Python strings; Postgres sorts under the
  database collation, and the cursor predicate is evaluated in SQL. A unit test
  walking pages against the fake proves the use case threads the cursor, not
  that the walk is exhaustive.
  → Mitigation: exactly-once-across-pages, the duplicate-name tie-break, and
  cursor/order agreement are proven in integration against real Postgres. The
  fake's job is limited to the use-case-level rules (filter fingerprint
  refusal, limit clamping, unfiltered-search refusal).

- **The fake also cannot model the access scope**, because the fake node
  repository has no view of grants and `FakeUnitOfWork` models no foreign keys.
  Scope is the security-relevant property of search, and it is precisely the
  one a unit test cannot establish here.
  → Mitigation: every scope assertion — another user's node absent, an ACTIVE
  grant present, a PENDING grant absent, a trashed node absent, and the same
  three for the tag inventory — is an integration test. This is the existing
  posture for search and is not made worse; it is made explicit.

- **Pagination makes exhaustive enumeration of a large shared subtree cheap** for
  a recipient holding one grant near its root.
  → Mitigation: none needed, and this is the reasoning, not a hand-wave: the
  scope predicate is unchanged, and such a recipient can already enumerate the
  same subtree by walking `list_children`, which is itself paginated. The change
  reduces the number of requests, not the set of reachable nodes. If that set is
  wrong, the fix is the grant, not the pagination.

- **The tag inventory aggregates a co-owner's vocabulary.** A recipient sees the
  tags on nodes shared with them, counted together with their own.
  → Mitigation: they can already read each of those tags individually on
  `NodeDetail` with `VIEWER`, so no tag is newly disclosed; the aggregate is a
  convenience over data already visible. Counts never include anything outside
  the caller's scope, which is what keeps the aggregate from being an oracle
  about the rest of the owner's tree.

- **The unanchored `ILIKE '%term%'` still cannot use an index**, so the first
  page of a name search is a scan of the scoped rows.
  → Mitigation: unchanged by this change, and each *subsequent* page is cheaper
  than the first, because the cursor predicate adds `normalized_name > …`, which
  the existing index can use to start the scan where the last page ended. Worth
  measuring on a realistic corpus; a trigram index is the known remedy and is
  its own change. Content search is not the remedy and stays out.

- **`tag_match=any` widens results, and a client that flips the mode mid-walk
  gets a `422` rather than a merged result.**
  → Mitigation: the page bound applies identically in both modes, and the
  refusal is the intended behaviour — the alternative is a page that silently
  belongs to neither walk.

- **Returning `NodePage` from search changes the response model** even though the
  JSON is a superset.
  → Mitigation: the added field is optional and `items` is spelled identically,
  so decoders that ignore unknown fields — including the generated clients this
  project's OpenAPI schema feeds — are unaffected. A client that pins the schema
  strictly regenerates.

## Migration Plan

None. No table, column, index, or backfill; nothing to roll back but code.
Existing clients keep working unchanged, and a client that never sends `cursor`
sees today's behaviour plus an honest `next_cursor` when there is more.

## Open Questions

- **Should `next_cursor` on a search be usable after the underlying nodes
  change?** The spec guarantees exactly-once only for a result set that does not
  change during the walk, which is the honest guarantee for keyset paging and
  matches what `list_children` already provides without saying so. Whether
  either surface should offer a snapshot (a transaction-scoped or
  timestamp-pinned walk) is a bigger question about read consistency across the
  whole API, not a search question.

- **Should the inventory report metadata keys too?** "Which keys are in use"
  is the same shape of question and the same one-line aggregate over
  `node_metadata`. It is left out because tags are a human vocabulary that a
  person has to recall, while metadata keys are written by an integration that
  already knows its own schema. If an integration turns out to need it, it is an
  additive endpoint alongside this one.
