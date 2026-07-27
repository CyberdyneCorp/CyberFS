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
entire purpose is independent concurrent writers, so each is addressed below: the
clobber by writing deltas rather than collections, and the colliding revision by
serializing the read the bump is computed from.

## Goals / Non-Goals

**Goals:**

- Add or remove individual tags, and set or delete individual metadata keys,
  without a read-modify-write round trip.
- Let two writers who touch disjoint labels both succeed, without coordinating
  with each other. The server may order them; the callers never have to.
- Make the reserved metadata namespace survive a caller's write, and stop showing
  it to a caller who can neither write nor remove it.

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

**The delta is applied as row-level statements, not as a whole-collection
replace.** `INSERT … ON CONFLICT DO NOTHING` for added tags,
`INSERT … ON CONFLICT (node_id, key) DO UPDATE` for set pairs,
`DELETE … WHERE tag IN (…)` for removals. The alternative -- compute the merged
collection and hand it to the existing `replace_*` -- would be correct under the
lock below and needs no new repository method, which makes it tempting. It is
still the wrong shape: `replace_*` deletes every row for the node, so a patch
would clobber whatever a writer that does *not* hold the lock did in between --
a `PUT`, or any label writer added later. Naming only the rows the request names
keeps a patch's blast radius equal to its delta whoever else is writing, which
is the property the endpoint is sold on. The unique constraints `ON CONFLICT`
needs are already in place.

**A patch serializes on the node it patches; lock-freedom is what is given up.**
`uow.lock_subtree(node_id)` -- the transaction-scoped Postgres advisory lock
`move` already takes (`application/nodes.py:344`) -- is the first statement of
the request, taken before the node row is read. Everything after it is inside
the critical section: the node read, the `If-Match` comparison, the read of the
current collection, the merge, the limit check, the writes, the revision bump.
Reading the node only after the lock matters twice over: it keeps the session's
identity map from handing back a pre-lock snapshot, and it is what makes the
returned ETag describe a state nobody could have moved underneath it.

This leans on the isolation level the engine already runs at. Nothing in
`infrastructure/db.py` sets one, so it is Postgres' default `READ COMMITTED`,
where each statement takes a fresh snapshot -- which is why the patch that waited
for the lock then reads what the patch ahead of it committed. Under
`REPEATABLE READ` the waiter would acquire the lock and still read the pre-lock
state, and the limit check would be back where it started. So the lock is only
half the mechanism, and raising the isolation level globally would silently
remove the other half.

The reason a lock is needed at all is that two of this change's promises are not
expressible as row-level statements. "A node holds at most `MAX_TAGS_PER_NODE`
tags" is a cardinality bound over rows that no per-row constraint states, and "a
patch that changes nothing writes nothing" is a judgement about the difference
between two states. Both must read the collection, and an unserialized read is
the lost update the row-level statements otherwise avoid: two patches on a node
holding 63 tags each read 63, each compute 64, both insert, and the node ends
with 65.

So the guarantee is that **disjoint patches do not lose each other**, and it
comes from delta semantics -- no statement clobbers a label it does not name --
not from lock-freedom. Concurrent patches on one node run one after the other
rather than at once, and two that would jointly cross a maximum do not both
succeed: the second reads the first's result and is refused. The spec names that
outcome rather than leaving it to be discovered.

**`PUT` takes the same lock, or the maximum is not a bound.** It is tempting to
leave `replace_tags`/`replace_metadata` unserialized on the grounds that a
replace states a complete collection and so has no merge to lose. That reasoning
covers the replace and misses the patch: a `PATCH` that reads 63 tags under the
lock while a `PUT` commits 64 outside it inserts its row against a state that no
longer exists, and the node ends with 65. The bound is a property of the node,
not of a verb, so every writer of the collection has to pass through the same
serialization point for any of them to be able to rely on it. Both replaces
therefore take `lock_subtree` in the same position the patches do. This is also
what lets the spec say the maximum holds, rather than that it holds between
partial updates -- a qualification no caller could act on.

**Why the lock precedes authorization.** Taking the lock as the very first
statement means an authenticated caller can park a transaction-scoped advisory
lock on a node id it has no rights to, by sending a well-formed body that will be
refused a moment later; because `_lock_key` folds the UUID into 31 bits, the id
need not even exist. Authorizing first would close that, and it is the obvious
reordering -- but `SqlNodeRepository.get` is `Session.get`, which serves the
identity map, so the node read that authorization performs would be the read the
critical section then depends on, taken *before* the lock. Every read after it
would be served the pre-lock row from the map, the limit check would be back to
deciding from a stale collection, and the serialization would look present while
being worthless. Closing it properly needs an expiry the repository port does not
expose, which is a wider change than this one. What is cheap and is done here:
the delta is validated before the lock, since validation is pure, so a body that
was never going to be accepted cannot reach the lock at all. The residual is a
caller able to serialize label writes on a node it cannot see -- a nuisance
bounded by one short request, on a lock namespace `move` already shares -- and it
is recorded rather than described as closed.

Two alternatives were rejected. **Enforcing the cardinality bound in the
database** with a trigger counting rows per node would keep the endpoint
lock-free, but it costs a migration this change otherwise does not need, it
moves a domain limit into DDL where the domain cannot see it (`MAX_TAGS_PER_NODE`
is a constant in `domain/nodes.py`, deliberately not configuration), and it does
nothing for the no-op promise, which would have to be dropped -- a trigger
cannot tell the service that nothing changed. **Leaving the pre-read
unserialized and weakening the limit** to something a node may temporarily
exceed is worse: a maximum a caller crosses by retrying is not a maximum, and
`PUT` would then refuse writes to a node whose state `PATCH` produced.

The cost is throughput on a single hot node, plus the lock key being a fold of
the node id into the signed 32-bit advisory space, so two unrelated nodes can
collide and serialize for nothing. The method is also named for its first
caller; it locks one node id, and reusing it leaves the port, the SQL adapter,
and the fake untouched. Its one incidental consequence -- a patch on `X`
serializing with a move into `X` -- is harmless, and a patch takes exactly one
lock, so it cannot deadlock with anything.

**The revision is bumped in Python under the lock, and only if something
changed.** `node.touch(now)` then `uow.nodes.update(node)`, exactly as the
replace does. A SQL increment (`revision = revision + 1`, the pattern the
recursive soft delete uses) was the earlier plan, on the grounds that two
concurrent patches would then get two distinct revisions instead of both writing
`N + 1`. The lock already produces that -- the second patch reads the node after
the first commits, so it reads `N + 1` and writes `N + 2` -- and the increment
carries a defect the pattern hides. The ETag a route publishes is `node.etag`,
`f'"{id.hex}-{revision}"'` computed from the in-memory `Node`
(`domain/nodes.py:222`) and reached through `_view` → `NodeDetail` → the `ETag`
header; a bulk `UPDATE` does not touch that object, so a successful `PATCH`
would answer with the *pre*-patch ETag and the caller's next `If-Match` would
`412`. (`soft_delete_subtree` escapes this only because it also calls
`node.soft_delete(now)` on each domain object -- it bumps in Python too, from a
value read earlier, which is exactly what a patch must not do outside a lock.)
Closing the gap would mean returning the new revision from the repository and
assigning it onto the aggregate: one more moving part for a guarantee the lock
already gives. So `PATCH` and `PUT` bump the same way, and differ only in that
`PATCH` does not bump when nothing changed.

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
that would exceed them is refused with the same error a `PUT` gives. The check
runs inside the critical section described above, against a collection read under
the lock, which is what makes it a bound rather than an advisory. All four lists
-- `add`, `remove`, `set`, and the metadata removal keys -- are additionally
bounded by the same maxima at the API edge, since no legitimate request names
more entries than a node could hold, and an unbounded list is unbounded work
before any check runs.

**Tag normalization applies to both directions.** `remove: ["URGENT"]` removes
`urgent`, because that is the tag -- the stored form is the only form. Metadata
keys are matched byte for byte for *equality* -- which key a set writes, which
key a removal names, which keys the request duplicates -- as they are everywhere
else. The one exception is the reserved-prefix test, which is casefolded because
`validate_metadata` already casefolds it (`key.casefold().startswith(...)`,
`domain/nodes.py:111`). Every place this change tests that prefix -- the `set`
list, the removal list, the `replace_metadata` preservation predicate, and the
filter that keeps reserved pairs out of a response -- casefolds identically. A
predicate that disagreed with the write-side guard would be a hole in the
namespace this change exists to close: `remove: ["CyberFS.trusted"]` must be
refused for the same reason `set` refuses it.

**The reserved namespace survives every write a caller makes, and is invisible to
one.** `remove` refuses a `cyberfs.`-prefixed key, and `replace_metadata` deletes
only the non-reserved pairs before inserting. Without the second half the guard
on `PATCH` would be theatre: a caller could clear system metadata with a `PUT` of
an empty list. The alternative -- letting a caller delete reserved keys -- makes
the namespace useless for its stated purpose, which is metadata CyberFS can trust
it wrote.

Preserving those pairs raises a question the read path has to answer, because
`metadata_for` selects every row for the node with no key filter
(`repositories.py:308-314`) and `labels_for` hands the result to every
`NodeDetail`. Left alone, the moment anything writes a reserved key it appears in
every `GET /nodes/{id}` and every metadata response, and a client that
round-trips the object it was just handed back through `PUT` gets a `422`,
because `validate_metadata` refuses reserved keys. So `labels_for` filters the
reserved prefix out of the caller-facing metadata. A caller sees exactly the pairs
it may write, "replace these pairs" still describes what it gets back, and the
round trip stays valid. The repository read stays unfiltered: that is how CyberFS
reads its own namespace, and how backup carries it. The rejected alternative --
returning reserved pairs read-only -- keeps the response a complete picture of the
table but breaks the echo, and a key a caller can see, cannot write, and cannot
remove is a worse thing to hand a client than a key it never sees.

Nothing writes such a key today, so both halves are invisible in practice; the
point is that they are true before something depends on them. A caller cannot
clear a reserved key by any route, and CyberFS itself writes through the
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
  real change, `PUT` always.
  → Mitigation: it is a specified difference, not an accident, with a scenario
  for each. The mechanism is now the same for both, so the only asymmetry left is
  the one the two verbs' meanings demand.

- **Patches to one node serialize**, so a hot node's label writes queue behind
  each other, and an unrelated node whose id folds onto the same advisory key
  queues with it.
  → Mitigation: the alternative was an unenforceable maximum (see Decisions). A
  label write holds the lock for a handful of statements with no external call in
  the critical section, and the change buys back far more round trips than it
  costs.

- **Concurrent patches on the same key still race**, in the sense that the second
  to run overwrites the first's value for that key.
  → Mitigation: the blast radius is one key rather than the whole map, which is
  the entire improvement being claimed. Nothing here promises that a caller's
  value for a key survives another caller's write to the same key, and the spec
  says so rather than implying otherwise.

- **A metadata search can still name a reserved key**, so once something writes
  one, a caller who guesses the key -- and, with `value`, guesses the value
  exactly -- can confirm it on a node they may already see, even though the key
  is filtered out of every response.
  → Mitigation: no reserved key exists in any deployment, and search returns
  nodes, never values, so the leak is an exact-guess oracle over nodes already
  visible to the caller. Narrowing the search filter is an observable change to a
  shipped endpoint and belongs to whichever change first writes a reserved key,
  which will know what secrecy that key actually needs. Recorded here so it is a
  decision rather than an oversight.

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
  later. Both halves are stated in the spec: that a reserved pair survives a
  replace, and that it never appears in a response -- so a caller's view of its
  own metadata is still complete and still echoable, while the table's post-state
  is deliberately not what a caller is shown.

- **The no-op's cache exemption needs no change to `caching`.** That requirement's
  "Invalidation on mutation" drops a node's cached entry when "a node's metadata or
  content changes", and a no-op patch changes neither, so the exemption follows
  from the existing requirement rather than carving one out of it -- which is why
  the delta states it on the partial-update requirement and leaves `caching`
  alone. Because it is the one guarantee here that a fake cannot show, it is
  pinned in the tier where Redis is real.

## Migration Plan

None. No schema change: `node_tags` and `node_metadata` already carry the unique
constraints `ON CONFLICT` needs and the `node_id` cascade purge relies on. Purely
additive at the API: two new methods on existing paths, and every existing
request behaves as before except that a `PUT` of metadata no longer deletes
reserved keys, of which there are none. Rolling back means removing the two
routes; nothing stored by them is unreadable afterwards, since a patched
collection is indistinguishable from a replaced one.

**A patch on a trashed node is a `404`, and it is already decided.** A trashed
node is invisible to search, so labelling it has no effect a caller can observe
until it is restored, and the question needed no judgement: `_authorize` raises
`NotFoundError` whenever `node.is_deleted` (`application/nodes.py:104-114`), the
same response as "no permission" so a probe cannot tell a trashed node from an
invisible one. A patch goes through `_authorize` like `rename` and `move`, so it
answers `404` without any code of its own. It is written into the spec so the
behaviour is specified rather than emergent, and pinned with a test.

## Open Questions

None outstanding.

## Notes on points raised in review

Two review observations were accurate in substance but wrong in detail, and the
detail matters to whoever implements this:

- The ETag is `Node.etag`, not `Node.version_token`; there is no
  `version_token`. The staleness argument is unaffected -- it is built from
  `revision` either way.
- `soft_delete_subtree` is not an example of a SQL increment leaving the
  aggregate stale: it re-applies the bump to each domain object in Python
  afterwards. It is therefore the wrong pattern to copy for a patch, for the
  opposite reason to the one given -- not because it ignores the new value, but
  because it recomputes it from a value read earlier.
