## Context

`NodesService.replace_tags` and `replace_metadata` authorize `EDITOR`, check
`If-Match`, validate the whole collection, call
`SqlNodeRepository.replace_tags` / `replace_metadata` -- each a `DELETE` of every
row for the node followed by inserts -- then `node.touch(now)`, `update(node)`,
audit, and invalidate the cache. Labels live in `node_tags(node_id, tag)` and
`node_metadata(node_id, key, value)`, unique on their natural key and cascading
on `node_id`. Search ANDs one `EXISTS` per tag plus one for the metadata pair,
scoped to nodes the caller owns or holds a non-pending grant on, excluding
trashed nodes.

Two properties of that implementation matter here. The `DELETE`-then-insert makes
every write a whole-collection clobber, so a stale read becomes a lost update.
And `node.touch()` bumps the revision in Python from a value read earlier, so two
concurrent writers can both persist `revision = N + 1` -- the ETag then fails to
distinguish two different states. Both are tolerable for a replace, whose caller
is asserting a complete state anyway. Neither is tolerable for the verb whose
entire purpose is independent concurrent writers.

## Goals / Non-Goals

**Goals:**

- Add or remove individual tags, and set or delete individual metadata keys,
  without a read-modify-write round trip.
- Let two writers who touch disjoint labels both succeed, without coordination.
- Make the reserved metadata namespace survive a caller's write.

**Non-Goals:**

- **Changing `PUT`.** Its replace semantics stay, apart from no longer deleting
  reserved keys. Callers depend on "these are now the tags", and a merge-only
  world cannot say that.

- **Per-version tags or metadata.** The original change rejected this and the
  rejection holds. Labels describe the node; a version describes content. Beyond
  the conceptual line, three concrete problems: search would have to decide which
  version's labels make a node match, and every answer is wrong (the current
  version's makes history unsearchable; any version's makes a node match a label
  it no longer carries); a restore would have to decide whether labels roll back
  with the bytes, and neither answer is defensible for a label like `approved`;
  and row counts would multiply by `VERSION_RETENTION_COUNT` for a filter that
  only ever returns nodes. A file whose *content* needs describing per revision
  wants a metadata key holding the version id, which the current shape already
  permits.

- **Tag rename and merge across a tree.** Genuinely useful and deliberately not
  here. It does *not* need the `ASYNC_REWRAP_THRESHOLD_NODES` pattern: that
  threshold exists because rewrapping is per-node cryptographic work with an
  external key operation each, and because a *pending* grant is a safe
  intermediate state -- it confers nothing, so a half-finished share is invisible
  rather than wrong. A tag rename has neither property. It is one set-based
  `UPDATE` with no external calls, and it has no safe half-state: a half-renamed
  subtree answers a search for the old tag *and* the new one with partial truth,
  and there is no flag that makes that invisible. So the async pattern would add
  a worker, a queue row, and a visible wrong state to a statement that does not
  need one.

  What it does need is a decision this change is not the place to make: a subtree
  contains nodes the caller may not edit, so a tree-wide rename is a *partial*
  mutation reporting how many nodes it touched and silently skipping the rest --
  a shape no endpoint in CyberFS has yet. It also bumps N revisions and would
  flood the activity feed unless it emits one record for the operation instead of
  one per node, which is a departure from "changing tags is a node mutation".
  Those are the interesting questions, and they deserve a proposal that answers
  them rather than a paragraph here.

- **Tag autocomplete, tag listing, and usage counts.** Still separate, still
  useful.

- **The S3 surface.** It exposes no tagging operation today, so there is nothing
  to extend; `PutObjectTagging` would be its own change with its own mapping
  question.

## Decisions

**Explicit `add`/`remove` lists, not a sentinel and not JSON Merge Patch.** The
original design named the sentinel problem: a merge document cannot express
removal without giving some in-band value the meaning "delete". RFC 7396 spells
that `null`, and for metadata it would technically work, since values are strings
and `null` is not a legal one. It is still the wrong shape here, for two reasons
that have nothing to do with ambiguity. First, tags are a *set*: a merge
document replaces arrays wholesale and has no per-element removal at all, so tags
would need a bespoke shape regardless, and having the two collections patched by
two different mechanisms is worse than having both patched by one. Second, a
removal hidden in a value slot cannot be validated as a removal -- the
reserved-namespace check below has to inspect what is being deleted, and an
explicit `remove` list is what makes that possible. Explicit lists also let the
server refuse a contradiction (`add` and `remove` naming the same tag) that a
merge document cannot even express.

**A patch that names the same label in both directions is refused.** Not
"remove wins", not "add wins". Either rule is a coin flip the caller did not
ask us to make, and the request is far more likely to be a bug in the caller
than an intent. `422`, nothing changed.

**An empty patch is refused.** A body with nothing in `add`, `remove`, or `set`
is a read dressed as a write. `GET` already exists.

**Merge happens in SQL, not in the service.** `INSERT … ON CONFLICT DO NOTHING`
for added tags, `INSERT … ON CONFLICT (node_id, key) DO UPDATE` for set pairs,
`DELETE … WHERE tag IN (…)` for removals. The alternative -- read the collection,
merge in Python, call the existing `replace_*` -- is a read-modify-write inside
the transaction, which is exactly the race the endpoint exists to remove: under
the default isolation level two such transactions each read the pre-state and the
second's replace deletes the first's insert. Row-level statements make disjoint
patches commute, and the unique constraints that make `ON CONFLICT` work are
already in place.

**The revision is bumped by a SQL increment, and only if something changed.**
`UPDATE node SET revision = revision + 1, updated_at = :now` in the same
transaction, rather than `node.touch()` on a value read earlier. Two concurrent
patches then produce two distinct revisions instead of both writing `N + 1`,
which matters far more for a verb built for concurrent writers than for a
replace. The recursive soft delete already bumps this way, so the pattern is not
new. `PUT` keeps its Python bump: changing it is a behavioural change to a
shipped endpoint that this change does not need, and it is a small, separate
cleanup.

**A no-op patch is a success that writes nothing.** Adding a tag the node already
carries, or removing one it does not, leaves the collection identical: no
revision bump, no audit record, no cache invalidation, `200` with the current
state and the unchanged ETag. Bumping anyway would invalidate every other
client's ETag for a change none of them can observe, and would make two agents
that each add the tag the other already added invalidate each other forever.
`PUT` is unaffected and keeps bumping unconditionally -- a caller asserting a
complete state has a state in mind, and the asymmetry is the honest consequence
of the two verbs meaning different things: a replace is an assertion, a patch is
an increment, and an empty increment is nothing.

**`If-Match` is checked before the effect is computed.** A stale token is `412`
even when the patch would have been a no-op. The precondition is a statement
about the caller's view of the node, not about the outcome; answering `200`
because we happened to agree would tell a caller their view was current when it
was not. And a patch that supplies `If-Match` is asking for
last-writer-detection, which is a legitimate thing to want even here.

**Limits are validated after the merge, against the resulting collection.**
`MAX_TAGS_PER_NODE` and `MAX_METADATA_PAIRS` bound what a node holds, so a patch
that would exceed them is refused with the same error a `PUT` gives. The
`add`/`set` lists themselves are additionally bounded by the same maxima at the
API edge, since no legitimate request names more entries than a node could hold,
and an unbounded list is unbounded work before any check runs.

**Tag normalization applies to both directions.** `remove: ["URGENT"]` removes
`urgent`, because that is the tag -- the stored form is the only form. Metadata
keys are matched byte for byte in both directions, as they are everywhere else.

**The reserved namespace survives writes that do not name it.** `remove` refuses
a `cyberfs.`-prefixed key, and `replace_metadata` now deletes only the
non-reserved pairs before inserting. Without this, the guard on `PATCH` would be
theatre: a caller could clear system metadata with a `PUT` of an empty list. The
alternative -- letting a caller delete reserved keys -- makes the namespace
useless for its stated purpose, which is metadata CyberFS can trust it wrote.
Nothing writes such a key today, so the change is invisible in practice and the
point is that it is true before something depends on it. A caller therefore
cannot clear a reserved key by any route; CyberFS itself writes through the
repository, not the endpoint.

**No new audit action.** A patch emits `NODE_TAGS_CHANGED` or
`NODE_METADATA_CHANGED`, the actions the replace already emits, both already in
`ACTIVITY_ACTIONS`. Distinguishing the verb in the action would double the
enumeration for a distinction nobody reading an activity feed cares about, and
every new action is a classification that must be made explicitly or default to
being retained forever. The context records how many entries were added and
removed; it does not record the tag or key text, which would copy user-supplied
label content into records kept on a different clock.

**Response is `NodeDetail`, as for `PUT`.** The resulting collection and the new
ETag come back in the same response, which is what makes the follow-up read
unnecessary and closes the round trip this change exists to remove.

## Risks / Trade-offs

- **Two writers no longer detect each other.** That is the feature, but it means
  a patch without `If-Match` cannot report that someone else also changed the
  labels. A workflow that needs to see the whole collection it is reasoning about
  gets a stale picture.
  → Mitigation: `If-Match` still does exactly what it always did, and the
  response carries the post-merge collection, so a caller who needs certainty
  has both a precondition and an immediate answer.

- **The two verbs bump the revision by different rules** -- `PATCH` only on a
  real change and via a SQL increment, `PUT` always and via the Python bump.
  A client reading the code could be surprised.
  → Mitigation: it is a specified difference, not an accident, with a scenario
  for each; and unifying `PUT` onto the increment is a small follow-up whose only
  obstacle is that it changes a shipped endpoint's revision behaviour for no
  benefit to this change.

- **Concurrent patches on the same key still race**, and last writer wins on
  that key.
  → Mitigation: the blast radius is one key rather than the whole map, which is
  the entire improvement being claimed. Nothing here promises per-key
  serialization, and the spec says so rather than implying otherwise.

- **A no-op patch returns `200` and looks like a write in a client's logs**
  while leaving no audit record, so an operator reconstructing what happened
  from activity will not see it.
  → Mitigation: intended, and the same is already true of every read. An
  operation that changed nothing is not something an activity feed should have to
  carry, and a feed full of no-ops is worse evidence than one without them.

- **The reserved-namespace preservation changes `PUT`'s observable behaviour**:
  a `PUT` of an empty list previously emptied the table for that node and now may
  leave rows behind.
  → Mitigation: no key in that namespace exists in any deployment, so no `PUT`
  can behave differently today; the change is provably inert now and correct
  later. It is stated in the spec so the response no longer being the complete
  post-state of the table is a documented property.

## Migration Plan

None. No schema change: `node_tags` and `node_metadata` already carry the unique
constraints `ON CONFLICT` needs and the `node_id` cascade purge relies on. Purely
additive at the API: two new methods on existing paths, and every existing
request behaves as before except that a `PUT` of metadata no longer deletes
reserved keys, of which there are none. Rolling back means removing the two
routes; nothing stored by them is unreadable afterwards, since a patched
collection is indistinguishable from a replaced one.

## Open Questions

**Should a patch be allowed on a trashed node?** A trashed node is invisible to
search, so labelling it has no effect a caller can observe until it is restored,
and `rename` and `move` already refuse. Leaning toward refusing with the same
error for the same reason -- consistency with the neighbouring mutations beats a
capability nobody has asked for -- but it should be settled against the actual
behaviour of the authorization path during implementation, and pinned with a
test either way.
