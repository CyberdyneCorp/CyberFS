## Context

Nodes today carry name, kind, size, content type, encryption state, timestamps
and a revision. Search is a single `ILIKE '%term%'` on `normalized_name`, scoped
to nodes the caller owns or holds an active grant on, excluding trashed nodes
(`SqlNodeRepository.search_by_name`).

`FileVersion.plaintext_digest` already holds the SHA-256 of the plaintext, and
`file-storage/spec.md` already lists "digest" among the metadata the API must
expose -- but no response schema carries it. That half of this change is
finishing something, not starting it.

## Goals / Non-Goals

**Goals:**

- Label a node and record structured facts about it.
- Find nodes by tag or metadata as cheaply as by name.
- Expose the digest the spec already requires, to callers entitled to it.

**Non-Goals:**

- Full-text search over content. The invariant that content is never indexed is
  load-bearing for the encryption story and stays.
- Searching by digest. Useful for deduplication, but it turns the digest into a
  lookup key, which is the confirmation-attack shape this change avoids
  elsewhere. Worth its own consideration.
- Per-version tags or metadata. These describe the node; versions describe
  content.
- Renaming or merging tags across a tree, and tag autocomplete. Both are useful
  and both are separate.
- Partial (`PATCH`) updates. See below.

## Decisions

**Tags and metadata are separate concepts, not one map.** A tag is a label with
no value, matched exactly, and answers "show me everything marked X" — the query
people actually run. Folding tags into metadata as a reserved key forces that
query to become a substring match inside a value, which cannot use an index and
degrades exactly as the collection grows. Two shapes cost a little API surface
and buy a search that stays fast.

**Two tables, not JSONB.** `node_tags(node_id, tag)` and
`node_metadata(node_id, key, value)`, each unique on their natural key and
cascading on `node_id`. Postgres `jsonb` with a GIN index would work and mean one
fewer join, but rows make the limits enforceable with a plain count, keep the
search a straightforward join rather than containment operators, and match how
every other collection in this codebase is stored. The cascade also means purge
and delete need no new cleanup: `PublicLinkRow` and `GrantRow` already rely on
the same mechanism.

**Replace, don't merge.** `PUT /nodes/{id}/tags` and `PUT /nodes/{id}/metadata`
each replace the whole collection. Replacement is idempotent and makes "remove
the last tag" expressible, which a merge-only `PATCH` cannot do without a
sentinel. A partial update is read-modify-write with `If-Match`, which the
revision bump already supports. `PATCH` can be added later without breaking this.

**`EDITOR` writes, `VIEWER` reads.** Tags and metadata describe the content, and
anyone trusted to change the content is trusted to describe it. This matches
`rename` and `move`, which also take `EDITOR`. Restricting writes to the owner
would make a shared folder undescribable by the people working in it.

**Both are node mutations.** They bump `revision`, honour `If-Match`, and emit an
activity record. A caller holding an ETag must not have it silently invalidated
by a change they cannot see, and a label change is exactly the kind of edit
someone later wants to attribute.

**The audit actions are activity, not security records.** `SECURITY_ACTIONS` is
derived as everything not listed in `ACTIVITY_ACTIONS`, so the new actions must
be added to the activity set explicitly or they will be retained forever by
default. Tagging a file is an ordinary operation and belongs with uploads and
renames; the derivation's safe direction means forgetting is the safe mistake.

**Tags are normalized; metadata keys and values are not.** A tag is a
user-facing label whose whole purpose is to match, so case and surrounding
whitespace are noise. A metadata value is data an integration wrote and expects
back byte-for-byte; normalizing it would corrupt identifiers. Keys are matched
exactly for the same reason.

**Filters AND together, including repeated tags.** `?tag=a&tag=b` means "carries
both". Every filter narrows, which is what a filter list is understood to do and
what keeps a long query from accidentally returning everything. OR is
expressible by issuing two searches; AND is not expressible by any number of OR
queries.

**A reserved key prefix.** Keys beginning with the system namespace are refused
from callers, so metadata written by CyberFS itself can never be forged by a
user. Nothing writes such keys yet; the guard exists so the namespace is
available later without a migration or an ambiguity about provenance.

**Limits are constants, not settings.** Maximum tags per node, tag length,
maximum pairs, key and value lengths. They bound the join fan-out and the row
growth. Making them configurable invites a deployment to raise them until search
degrades, and there is no evidence yet that any deployment needs different ones.

## Risks / Trade-offs

- **Tags and metadata are plaintext in the database.** Unavoidable while they are
  searchable, and the one place this change widens what a database compromise
  reveals. A user can put something sensitive in a tag as easily as in a
  filename.
  → Mitigation: state it in the specification and the user-facing documentation
  so it is a known property rather than an assumption. Content stays encrypted
  and unindexed.

- **Administrators can read tags and metadata, ungated**, where file names are
  withheld unless `ADMIN_SHOW_FILENAMES` is enabled and audited. A label can be
  as revealing as a name.
  → Mitigation: none in this change; it is the accepted decision. Recorded here
  and in the proposal so it is visible as a choice. The admin surface exposes no
  node-level endpoint today, so it takes effect only when one is added — which
  is the moment to revisit it.

- **Search cost grows with the filters.** Each tag in an AND is a join or a
  correlated existence test, so a query naming many tags does more work.
  → Mitigation: the limits bound how many can be supplied, results stay bounded
  by `PAGE_SIZE_MAX`, and both tables are indexed on the columns searched. Worth
  measuring on a realistic corpus rather than assuming.

- **Exposing the digest is irreversible in practice.** Once clients read it, a
  later decision to withhold it is a breaking change.
  → Mitigation: it goes only to callers who can already read the bytes and
  compute it themselves, and is kept off the admin surface, which is where it
  would reveal something new.

## Migration Plan

One Alembic migration creating `node_tags` and `node_metadata`, both empty and
both cascading on `node_id`. Additive: existing rows are untouched, every new
field is optional in responses, and the search parameters are optional, so an
existing client sees no change. Rolling back means dropping the two tables and
losing whatever was written into them; nothing else depends on them.

## Open Questions

**Resolved during implementation: a copy carries no tags or metadata.** `copy`
already carries no grants, because the copy belongs to the caller and
`sharing/spec.md` requires it to be visible only to its new owner. A copy may
therefore cross owners -- a viewer of someone else's file can copy it into their
own tree -- and inheriting labels would import the source owner's assertions into
the copier's namespace by way of a read. Dropping them matches the existing
posture that a copy takes the content and the name and nothing else. Pinned by
`test_a_copy_does_not_inherit_labels`. Purge destroys them via the cascade, which
was never in question.
