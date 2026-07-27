## Why

Labels can only be written wholesale. `PUT /nodes/{id}/tags` and
`PUT /nodes/{id}/metadata` replace the collection, which was the right first
move -- replacement is idempotent and it is the only shape that can express
"remove the last entry" without inventing a sentinel.

What it cannot express is a contribution. Adding one tag means reading the node,
merging locally, and writing the whole set back, and every such round trip is a
lost-update window: two writers who each add a different tag produce a node with
one of them. The people who label nodes at scale are exactly the ones who collide
-- an ingest pipeline stamping `source=…`, a classifier adding `pii`, a user
tagging `urgent` -- and each is a separate writer with no knowledge of the
others' labels. Today they must either serialize behind `If-Match` and retry, or
quietly clobber one another.

A second, smaller thing surfaces while designing removal: the reserved
`cyberfs.` namespace is only half reserved. A caller cannot *write* a key in it,
but a `PUT` deletes every existing pair before writing the validated ones, so a
caller can already remove system-written metadata. Nothing writes such a key yet,
so nothing is broken -- but the namespace exists so that CyberFS can trust what
it finds there, and a namespace a user can empty is not trustworthy.

## What Changes

- **`PATCH /api/v1/nodes/{node_id}/tags`** taking `add` and `remove` lists.
- **`PATCH /api/v1/nodes/{node_id}/metadata`** taking `set` pairs and `remove`
  keys.
- Both **write at the row level**: a tag is inserted with
  `ON CONFLICT DO NOTHING` and deleted by name, so a patch never touches a label
  it did not name and two patches naming different tags both survive. This is the
  point of the change.
- **A patch with no effect changes nothing** -- no revision bump, no audit
  record, no cache invalidation. Adding a tag a node already carries is a
  success, not a write.
- **Limits are checked after the merge.** Adding one tag to a node already at
  `MAX_TAGS_PER_NODE` is refused with the same error a `PUT` would give.
- **Patches to one node serialize** on the advisory lock `move` already uses.
  Checking a per-node maximum, and deciding that a patch changed nothing, both
  require reading the collection first, and an unserialized read is the lost
  update this endpoint exists to remove. Disjoint patches still both land; they
  simply land one after the other, and two that would jointly cross a maximum do
  not both succeed. design.md records why the alternative -- a counting trigger in
  the database -- was rejected.
- **The reserved namespace becomes genuinely reserved**: `remove` refuses a
  `cyberfs.`-prefixed key, `PUT` now *preserves* pairs in that namespace instead
  of deleting them, and no response shows them -- so the metadata a caller is
  handed stays exactly what it may write back.
- `EDITOR` to write, `If-Match` honoured, revision bumped, activity recorded --
  all exactly as `PUT`, reusing `NODE_TAGS_CHANGED` and
  `NODE_METADATA_CHANGED`.

Not changing: `PUT`'s replace semantics, tag normalization, the limits
themselves, the search filters, or the rule that **file content is never indexed
or matched**.

## Capabilities

### New Capabilities

None. This extends `file-storage`.

### Modified Capabilities

- `file-storage`: gains "Partial label updates", and "Key/value metadata" gains
  the statements that the reserved namespace survives every write a caller makes
  and never appears in a response to one.

## Impact

**Affected code:**

- `src/cyberfs/domain/labels.py` (new) -- validation for a tag delta and a
  metadata delta, reusing the existing constants and per-entry rules from
  `nodes.py`; a helper that refuses a request naming the same tag or key in both
  directions; the merge as a pure function; and the single reserved-prefix
  predicate every other place in this change tests through. A module of its own
  rather than more of `nodes.py`: a delta is validated in both directions, can
  contradict itself, and has its limits checked against its result rather than
  against itself, so it is a different kind of thing from a collection.
  `nodes.py` keeps the constants and is not modified.
- `src/cyberfs/adapters/outbound/db/repositories.py` -- `add_tags`,
  `remove_tags`, `set_metadata`, `remove_metadata_keys`; the existing
  `replace_metadata` learns to leave reserved keys alone.
- `src/cyberfs/domain/ports/repositories.py` and `tests/unit/fakes.py` -- the
  four new repository methods. The lock the patches take is already on the port.
- `src/cyberfs/application/nodes.py` -- `patch_tags` and `patch_metadata`, and
  `labels_for` stops handing reserved pairs to a caller.
- `src/cyberfs/adapters/inbound/api/schemas.py` and `routers/nodes.py` -- the
  two request bodies and the two routes.

**No new audit actions**, so nothing has to be classified: both patches emit the
actions `PUT` already emits, which are already in `ACTIVITY_ACTIONS`. The audit
context carries *counts* of what was added and removed rather than the tag and
key text, so label content is not copied into a second store with a different
retention.

**No new configuration and no new constants.** A patch body is bounded by the
same per-node maxima, because a request cannot legitimately name more tags than a
node could hold.

**No migration.** The tables, their unique constraints, and their `node_id`
cascade already exist; `ON CONFLICT DO NOTHING` needs the constraint that is
already there.

**Nothing changes about what a database compromise reveals.** Tags and metadata
were already plaintext and already ungated on the admin surface. No digest is
touched, so the withholding of the plaintext digest from `/api/v1/admin/*` is
unaffected.
