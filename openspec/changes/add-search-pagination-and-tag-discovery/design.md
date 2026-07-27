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

Two facts about the existing mechanism shape the decisions below. First, the
codec lives in the adapter: `encode_cursor`/`decode_cursor` are module-level
functions in `adapters/outbound/db/repositories.py`, and the application layer
has no way to reach them — nothing under `application/` imports `adapters/`, and
`tests/unit/test_layering.py` guards the pure layers. Second, the two bounds on a
page are not one bound: the route declares
`limit: Annotated[int, Query(ge=1, le=1000)] = 100`, so FastAPI refuses an
oversized limit before any use case runs, while `NodeService` separately clamps
with `min(limit, self._page_size_max)` against the configured `PAGE_SIZE_MAX`
(default `1000`, a setting, not a constant).

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
- **Deriving the routes' `limit` bounds from settings.** Both the default `100`
  and the ceiling `le=1000` are literals on the route: the same ceiling appears on
  `list_children`, the three admin listings, and the activity feed, and the same
  default on `list_children` and the admin listings. Changing them on the search
  route alone trades a small inconsistency for a worse one,
  and deriving a FastAPI `Query` bound from `Settings` means the OpenAPI schema
  stops being a static property of the code. Both stay; see "The page bound has
  two layers" below for what that means for the spec.
- **Making a descendant of a shared folder findable by search.** Search's scope
  predicate is `owner_id = <caller> OR id IN (<active grant node ids>)` — direct
  grant rows only. Read authorization, by contrast, resolves through an ancestor
  walk, so a file inside a folder shared with the caller is readable with `GET`
  and invisible to search. Closing that gap means an ancestor-aware scope
  predicate (a recursive CTE per search, or a materialized closure), which is a
  query-shape and index decision of its own with a real cost model. This change
  states the current behaviour as a scenario instead of quietly widening it —
  see "Search's scope is the granted node itself" below.

## Decisions

**Reuse the keyset cursor, do not add offset paging.** `?offset=` is less code
and every client understands it, and it is wrong here for two reasons that
compound: the cost of page *n* grows with *n*, because Postgres must produce and
discard the rows before it; and a node created, renamed, or trashed during the
walk shifts every later row, so pages silently duplicate or skip. Keyset paging
has neither problem, and this codebase already has exactly one pagination
mechanism. A second one would be the thing future readers have to hold in their
heads.

Concretely, "reuse" means: the same `Page[T]` return type, the same `_paginate`
sentinel-trimming helper, the same `\x1f`-joined base64 cursor payload, and the
same `ValidationError → 422` mapping. The only genuinely new pieces are the
filter fingerprint and a `(normalized_name, id)` predicate modelled on
`_child_cursor_predicate`. `list_children`'s signature, its cursor payload, and
its behaviour are untouched, so a listing added in parallel — the trash view is
building a cursor-paginated `list_trash_entries` on the `NodeRepository` port right
now — keeps working against `_paginate` and the cursor codec exactly as it does
today.
Where this change moves the codec (below), it re-exports it from its old home for
that reason.

**The page bound has two layers, and the spec describes the pair rather than
either one.** A limit above the route's `le=1000` never reaches a use case:
FastAPI answers `422` first. A limit below that ceiling but above a configured
`PAGE_SIZE_MAX` does reach the use case and is clamped by
`min(limit, self._page_size_max)`. With the default settings the two coincide at
`1000`, so the clamp is dead code unless an operator lowers `PAGE_SIZE_MAX` —
which is exactly what that setting is for. Writing "a limit above `PAGE_SIZE_MAX`
SHALL return at most `PAGE_SIZE_MAX` results" as a scenario would therefore have
been false on a default deployment, because `limit=1001` is refused rather than
reduced. The scenario says instead that an over-large limit is either refused or
reduced, never served in full, and that a reduced page still carries a cursor
when matches remain. Both halves are then testable: the refusal at the route, the
clamp at the use case.

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

**The fingerprint is computed and compared in one place, and that place is not
the repository.** Two layers could plausibly own the comparison, and picking
wrong makes the rule untestable:

- *In the adapter.* `SqlNodeRepository.search` decodes the cursor, compares the
  digest, raises. Rejected: the fake node repository would then need its own
  copy of the codec and its own copy of the fingerprint, so every unit test of
  the refusal would assert against the copy instead of against production code.
  `FakeNodeRepository.list_children` ignores `cursor` outright today and never
  sets `next_cursor`, which is how much fake there would be to write. That is
  precisely the failure mode the first risk bullet below warns about, aimed at
  the one rule the fake could otherwise pin honestly.
- *In the use case, over a shared codec.* Chosen. `NodeService.search` builds the
  filter-set value object, and when a cursor is present decodes it and compares
  the digest it carries against the digest that filter set implies, raising
  `ValidationError` on either a malformed cursor or a mismatch. The repository is
  handed the filter set and an already-decoded sort key
  (`normalized_name`, `id`), and its job is the `WHERE` predicate and the
  `ORDER BY` — no cursor parsing, no digest.

That requires the codec to be reachable from `application/`, so
`encode_cursor`/`decode_cursor` move to `src/cyberfs/domain/pagination.py` — the
pure layer that already owns the `Page` type they serve — joined by a small keyed
variant that prepends a fingerprint and refuses a payload whose fingerprint does
not match the one supplied. `adapters/outbound/db/repositories.py`
re-exports both names, so `activity_queries.py`, the audit feed, the admin
listings, and the trash listing under construction see no change at all — the
move is a relocation, not a rewrite, and the base64/`\x1f` payload format is
byte-identical. The keyed variant is deliberately generic: any future listing
whose scope lives in the query string rather than the path can bind its cursor
the same way, and the tag inventory in this very change is its second caller.

The fake's remaining obligation is small and honest: order by
`(normalized_name, id)`, slice after the decoded key, and build `Page` with the
shared codec. It reimplements neither the fingerprint nor the cursor format.

**Search's scope is the granted node itself, not the subtree under it, and this
change writes that down.** `file-storage/spec.md`'s requirement prose promises
searching "within the subtrees a caller may access", while its two scope
scenarios say "only nodes the caller owns or has been granted" — and the code
implements the scenarios: `owner_id = <caller> OR id IN (<active grant node
ids>)`. A grant is recorded on the root of a shared subtree, not on every
descendant (`shared_with_me` documents exactly that: "the roots of each shared
subtree, not every inherited descendant"), so a file inside a shared folder is
readable with `GET` and absent from search. The prose reads like a promise the
code does not keep.

This change adds a scenario saying the narrow thing plainly — the granted node
itself appears, its descendants do not — so the gap is a documented, testable
decision rather than a discrepancy a reader has to discover by running the query.
It is a clarification, not a narrowing: no scenario changes meaning and no
behaviour changes. Widening it is a non-goal above, with the reason.

Two consequences follow. The pagination risk analysis has to stop claiming that
paging makes a shared subtree cheap to enumerate through search — it cannot be
enumerated through search at all — and the tag inventory has to be scoped by the
same expression, not by an expression that happens to agree today.

**One scope predicate, used by both search and the inventory.** The inventory is
scoped exactly like search: owned, or an ACTIVE grant on the node itself, and not
trashed. Any other scoping would be a second access model to maintain. Stating
that is not enough, though, because two hand-written predicates drift and the
drift is invisible in unit tests — `FakeNodeRepository.search` ignores `subject`
entirely, so no fake can catch a scope difference, and the divergence would show
up only on shared trees in production. So the expression is extracted into one
private helper on `SqlNodeRepository` that both `search` and `tag_counts` call,
and the spec states the agreement as an invariant: a tag the inventory reports
with count *n* returns *n* nodes when used as a filter. Counts are therefore
per-caller, not global, and the spec says that too — otherwise someone will treat
the number as a property of the tag and be confused when two users disagree about
it. An integration test asserts the agreement for a node reached through a grant,
not only for owned nodes, because owned nodes are the case where two different
predicates would agree anyway.

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

The prefix is escaped with `_escape_like` before it is interpolated, the same way
the name search escapes its term. `normalize_tag` only applies NFC, strips, and
casefolds — it does not touch `%` or `_` — so an unescaped `?prefix=%` would
return the entire vocabulary and `?prefix=a_` would match `ab`. That is not a
scope escape, since the aggregate is scoped independently, but it is the endpoint
failing its own "tags beginning with it" contract, and it is the kind of thing
that quietly becomes a documented feature. A scenario pins the literal match so
nobody "fixes" the escaping away later, and the test that proves it is an
integration test: a fake matching a prefix with Python's `startswith` treats `%`
literally whether the SQL escapes it or not, so only real SQL can catch a missing
`_escape_like`.

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
a grant anywhere above it. That is a large amount of correctness risk bought with
an unmeasured amount of latency: the aggregate is one `GROUP BY` served by
`ix_node_tags_tag`, but it joins `node_tags` back to `nodes` to apply
`deleted_at IS NULL` and the scope, so it is not an index-only scan and this
change does not claim it is. Task 2.9 measures it. The conclusion holds either
way — if the aggregate turns out to be expensive, the answer is the index that
`EXPLAIN` asks for, proposed on its own, not a cache with a fan-out invalidation
rule. Grant listings and audit records are already read straight from Postgres
for comparable reasons.

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
  fake's job is limited to the use-case-level rules (filter fingerprint refusal,
  limit clamping, unfiltered-search refusal) — and those rules live in the
  application layer over the shared cursor codec, so the fake holds no copy of the
  fingerprint or the cursor format for a test to accidentally assert against.

- **The fake also cannot model the access scope**, because the fake node
  repository has no view of grants and `FakeUnitOfWork` models no foreign keys.
  Scope is the security-relevant property of search, and it is precisely the
  one a unit test cannot establish here.
  → Mitigation: every scope assertion — another user's node absent, an ACTIVE
  grant present, a PENDING grant absent, a trashed node absent, and the same
  three for the tag inventory — is an integration test. This is the existing
  posture for search and is not made worse; it is made explicit.

- **Pagination lets a caller collect everything search can return for a filter,
  in fewer requests than before.** Worth stating plainly, and worth stating
  narrowly: search returns owned nodes and the granted nodes themselves, so this
  is not a way to enumerate a shared *subtree* — the descendants of a shared
  folder are not in search's scope at all (see the scope decision above). What a
  recipient gains is the remainder of a page of matches they were already
  entitled to and could already have reached by walking `list_children`, which is
  itself paginated.
  → Mitigation: none needed. The scope predicate is unchanged, so the set of
  reachable nodes is unchanged; only the number of requests to cover it falls. If
  that set is wrong, the fix is the grant, not the pagination.

- **The tag inventory aggregates a co-owner's vocabulary.** A recipient sees the
  tags on nodes shared with them, counted together with their own.
  → Mitigation: they can already read each of those tags individually on
  `NodeDetail` with `VIEWER`, so no tag is newly disclosed; the aggregate is a
  convenience over data already visible. Counts never include anything outside
  the caller's scope, which is what keeps the aggregate from being an oracle
  about the rest of the owner's tree.

- **The unanchored `ILIKE '%term%'` still cannot use an index**, so the first
  page of a name search is a scan of the scoped rows.
  → Mitigation: unchanged by this change — the first page costs what a search
  costs today. The hope is that a *subsequent* page costs less, because the cursor
  predicate adds `normalized_name > …` and the planner can start the scan where
  the last page ended; that is a prediction, not a claim. Two things work against
  it: the scope predicate is an `OR` (`owner_id = … OR id IN (…)`) and the keyset
  predicate is itself a three-way `OR` (see `_child_cursor_predicate`), and either
  can cost an index-driven start. Task 2.9 runs `EXPLAIN` on a seeded corpus and
  records what the planner actually chose. A trigram index is the known remedy if
  it is needed, and it is its own change. Content search is not the remedy and
  stays out.

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

## Review Claims Rejected

One claim from the adversarial review was checked against the code and does not
hold, and acting on it would have made the delta worse:

- **"The new scope scenarios narrow the existing requirement."** They do not.
  `file-storage/spec.md` already carries two normative scenarios stating the
  narrow scope — "Search scoped to accessible nodes" ("only nodes the caller owns
  or has been granted") and "Tag and metadata search obeys the access scope"
  (same words). The new scenarios restate that scope and add the word *active*,
  which matches both the code (`pending.is_(False)`) and the existing
  pending-grant behaviour. Nothing is frozen that was not already normative. What
  *is* loose is the requirement prose at the top of "Listing, search, and
  metadata" — "within the subtrees a caller may access" — and the fix for a
  discrepancy between prose and scenarios is not to rewrite the scenarios but to
  say explicitly which one the system implements. That is what the added
  descendant scenario does. The rest of the same finding — that the descendant
  case is unspecified, and that the shared-subtree risk bullet described
  behaviour search does not have — was correct and is addressed above.

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
