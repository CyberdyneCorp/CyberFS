## Why

A file's name is the only thing a user can say about it, and the only thing they
can search on. There is no way to label a node, record where it came from, or
find it by anything other than a substring of its name.

The content digest is a related gap of a different kind: `file-storage/spec.md`
already requires node metadata to include the "digest", and every version already
stores `plaintext_digest` (SHA-256 of the plaintext). It is simply exposed
nowhere -- not on `NodeDetail`, not on `VersionSummary`. That is an unimplemented
requirement rather than a new feature.

## What Changes

- **Tags.** A set of short labels on any node, file or folder. Normalized,
  exact-matched, and indexed, because that is what makes filtering by them fast.
- **Metadata.** Key/value string pairs on any node, for facts an integration
  needs to record -- source system, external id, ingest batch. Matched by key, or
  by key and value together.
- **Search over all three.** `GET /api/v1/search` gains `tag`, `key`, and
  `value` filters alongside the existing name substring. Filters combine with
  AND, so each one narrows the result.
- **The digest is exposed** on `NodeDetail` (the current version's) and on
  `VersionSummary`, to callers who can already read the content.
- Writing tags or metadata requires `EDITOR`, matching rename and move. Reading
  them requires `VIEWER`. Both bump the node revision, so `If-Match` works.

Not changing: the existing name search, its scoping to owned and actively-granted
nodes, or the invariant that **file content is never indexed or matched**.

## Capabilities

### New Capabilities

None. This extends an existing `file-storage` requirement.

### Modified Capabilities

- `file-storage`: "Listing, search, and metadata" gains tags, key/value metadata,
  the filters that search them, the limits that bound them, and the digest
  exposure the requirement already called for.
- `content-encryption`: gains an explicit statement that tags and metadata are
  **not** encrypted, so their absence from the encryption boundary is a recorded
  decision rather than an oversight.

## Impact

**Affected code:**

- `src/cyberfs/domain/nodes.py` -- tag and metadata value objects, normalization,
  and the limits.
- `src/cyberfs/adapters/outbound/db/models.py` + a migration -- two new tables,
  `node_tags` and `node_metadata`, each cascading on `node_id`.
- `src/cyberfs/adapters/outbound/db/repositories.py` -- reads, writes, and the
  extended search query.
- `src/cyberfs/application/nodes.py` -- the use cases and their authorization.
- `src/cyberfs/adapters/inbound/api/` -- `PUT /nodes/{id}/tags`,
  `PUT /nodes/{id}/metadata`, the search parameters, and the digest fields.
- `src/cyberfs/domain/audit.py` -- `NODE_TAGS_CHANGED`, `NODE_METADATA_CHANGED`,
  classified as activity rather than security records.

**Tags and metadata are stored in plaintext.** They have to be: searchable means
indexed means readable by anything with database access. This is the one place
the change widens what a database compromise reveals, and it cannot be avoided
while keeping the feature. File content remains encrypted and unindexed.

**Administrators can see tags and metadata, ungated.** This is a deliberate
decision, recorded here because it diverges from how names are treated: names are
plaintext in the database too, but `admin-dashboard/spec.md` withholds them from
administrators unless `ADMIN_SHOW_FILENAMES` is enabled, and enabling it is
audited. Tags get no such gate, so a user's own labels are visible to operators
where a file name would not be. In practice the admin surface exposes no
node-level endpoint today, so nothing is revealed until one is added -- the
decision matters for whatever comes next, not for the current API.

**The digest is withheld from the admin surface.** It is a hash of plaintext, so
publishing it would let anyone holding it test whether a user has a specific
known file -- a confirmation attack that works even though the content is
encrypted. Owners and active share recipients can read the bytes anyway, so it
tells them nothing they could not compute.

**No new configuration** beyond the limits, which are constants rather than
settings until there is a reason to vary them per deployment.
