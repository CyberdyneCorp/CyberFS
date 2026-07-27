## 1. Domain

- [x] 1.1 Add tag normalization (trim, casefold) and validation in `src/cyberfs/domain/nodes.py`, refusing blank, whitespace-only, and over-length tags
- [x] 1.2 Add metadata key/value validation: non-empty key, key and value length limits, no duplicate keys, no reserved-prefix keys
- [x] 1.3 Define the limits as named constants (max tags per node, tag length, max pairs, key length, value length) with the reasoning at the definition
- [x] 1.4 Add `NODE_TAGS_CHANGED` and `NODE_METADATA_CHANGED` to `AuditAction`, and add both to `ACTIVITY_ACTIONS` so they are pruned rather than retained forever
- [x] 1.5 Confirm neither new action feeds a `SUMMARY_BUCKETS` counter

## 2. Persistence

- [x] 2.1 Add `NodeTagRow` and `NodeMetadataRow` to `models.py`, each cascading on `node_id`, unique on their natural key
- [x] 2.2 Index `node_tags.tag` and `node_metadata(key, value)` so filters do not table-scan
- [x] 2.3 Write the Alembic migration; `alembic upgrade head` verified -- it runs in
      CI and again on every deploy, and production is serving the new tables.
      `downgrade` is written but NOT exercised: no migration in this project has a
      downgrade test, so this one is no worse off than the other six, but the
      rollback path in design.md is unproven. Worth its own change.
- [x] 2.4 Add repository reads and writes: fetch for a node, replace for a node
- [x] 2.5 Extend `search_by_name` into a search accepting name, tags, and metadata, keeping the existing owned-or-granted scoping and trashed exclusion
- [x] 2.6 Confirm the cascade actually removes tags and metadata on purge, against real Postgres rather than the fake

## 3. Use cases

- [x] 3.1 `replace_tags` in `application/nodes.py`: authorize `EDITOR`, validate, replace, bump revision, honour `If-Match`, audit
- [x] 3.2 `replace_metadata`: the same shape
- [x] 3.3 Extend the search use case with the new filters, ANDing them
- [x] 3.4 Invalidate cached listings and metadata for the node on either write
- [x] 3.5 Decide whether `copy` carries tags and metadata (design.md, Open Questions) and pin the answer with a test

## 4. API

- [x] 4.1 `PUT /api/v1/nodes/{node_id}/tags` and `PUT /api/v1/nodes/{node_id}/metadata`
- [x] 4.2 Include `tags` and `metadata` on `NodeDetail`
- [x] 4.3 Add `digest` to `NodeDetail` (current version) and `VersionSummary` -- the requirement `file-storage/spec.md` already states
- [x] 4.4 Confirm no digest reaches any `/api/v1/admin/*` response
- [x] 4.5 Add `tag` (repeatable), `key`, and `value` query parameters to `GET /api/v1/search`
- [x] 4.6 Confirm the new routes and fields appear in the OpenAPI schema

## 5. Unit tests

- [x] 5.1 Tag normalization: case and whitespace variants converge on one stored form
- [x] 5.2 Tags are a set: duplicates collapse, order does not matter
- [x] 5.3 Blank, whitespace-only, and over-length tags are refused and change nothing
- [x] 5.4 Exceeding the tag limit is refused and changes nothing
- [x] 5.5 Duplicate metadata keys are refused rather than silently deduplicated
- [x] 5.6 Over-length keys and values are refused; the pair limit is enforced
- [x] 5.7 A reserved-prefix key is refused
- [x] 5.8 A `VIEWER` cannot write either collection; an `EDITOR` can
- [x] 5.9 Either write bumps the revision, and a stale `If-Match` is refused
- [x] 5.10 `NODE_TAGS_CHANGED` and `NODE_METADATA_CHANGED` are activity actions, not security records, and survive an activity prune only as long as other activity does

## 6. Integration tests

- [x] 6.1 Tags round-trip through the API and appear on `NodeDetail`
- [x] 6.2 Metadata round-trips and appears on `NodeDetail`
- [x] 6.3 Search by one tag returns the tagged node and nothing else
- [x] 6.4 Search by two tags returns only nodes carrying both
- [x] 6.5 Search by metadata key alone, and by key and value together
- [x] 6.6 Name substring combined with a tag narrows rather than widens
- [x] 6.7 Another user's tagged node never appears in the caller's results
- [x] 6.8 A recipient with an active grant does find the shared node by tag; a pending grant does not
- [x] 6.9 A trashed node is absent from tag and metadata search
- [x] 6.10 Results are capped at the page-size bound
- [x] 6.11 `digest` on `NodeDetail` and `VersionSummary` equals the SHA-256 of the uploaded bytes, for a plain file and an encrypted one
- [x] 6.12 Purging a node removes its tags and metadata rows (the cascade, against real Postgres)

## 7. End-to-end tests

- [x] 7.1 Against the deployment: tag a file, find it by tag, and confirm the digest matches the bytes uploaded
- [x] 7.2 Against the deployment: set metadata, search by key and value, and clean up by purging

## 8. Verification and documentation

- [x] 8.1 `just lint`, `just typecheck`, `just test-unit` clean
- [x] 8.2 `just test-integration` clean, verified in CI rather than assumed (run 429ffa4: 239 passed, 7 skipped, 0 failed -- up from 219, so all 20 new tests ran)
- [x] 8.3 `just test-e2e` clean against the deployment (31 passed against production)
- [x] 8.4 Document tags and metadata in `README.md`, stating plainly that they are stored unencrypted and readable by whoever can read the database
- [x] 8.5 Run `openspec validate add-node-metadata-and-search --strict`
