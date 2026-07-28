# Record the version id that content was sealed under

## Why

Copying or restoring a version of an **encrypted** file returns an empty body,
and has done since encryption landed in `db796fc`.

`EncryptionService.seal` mixes the version id into the AEAD
(`cipher.seal(plaintext, dek, version_id.bytes)`), which is what stops a frame
lifted out of one version from being replayed into another. `open` authenticates
with `version.id` — the id of the row it is serving. Two paths copy *already
sealed* bytes into a row that carries a different id:

- `ContentService.restore_version` — copies the source object to a new key and
  writes a new `FileVersion` with a fresh id.
- `ContentService._copy_content`, behind `POST /api/v1/nodes/{id}/copy` — the
  same shape, for a new node.

In both, `open` then authenticates against an id the bytes were never sealed
under. Decryption yields nothing and the endpoint answers `200` with an empty
body. Restore is the worse of the two: it also repoints `current_version_id`, so
a user rolling back an encrypted file watches it silently empty itself while
every byte is still in the object store, unreadable.

Nothing caught it because no test ever copied or restored a version of an
*encrypted* file — the copy and restore tests use plaintext, and the encryption
tests never copy. It was found by an integration test written for something else
entirely.

The defect is not that the AAD binds to a version. It is that the AAD was left
**implied** by a column that happens to share a name with it, so an operation
that legitimately gives content a new row silently changed the key material's
meaning.

## What changes

- `FileVersion` gains `seal_version_id`: the version id the bytes were sealed
  under, which for content sealed in place is the row's own id.
- `open` authenticates with `seal_version_id` rather than `id`.
- Copy and restore carry the **source's** `seal_version_id` onto the new row, so
  the bytes stay readable without being re-encrypted.
- A migration adds the column and backfills it to `id`, which is correct for
  every row that exists: every one of them was sealed in place, and any copy made
  so far was already unreadable.

Rejected alternative: **re-seal on copy**, opening the source stream with the
source id and sealing it again under the new one. Correct, and needs no
migration, but it decrypts and re-encrypts the whole object on every copy and
every version restore — turning a metadata operation into an O(size) crypto
operation — and it leaves the AAD just as implicit as it is now.

## Impact

- Affected specs: `content-encryption`, `file-storage`
- Affected code: `domain/nodes.py`, `application/content.py`,
  `application/encryption.py`, `adapters/outbound/db/models.py`,
  `adapters/outbound/db/mappers.py`, one Alembic migration
- No API shape changes. `seal_version_id` is internal and is deliberately **not**
  serialized: it is key material metadata, and a client has no use for it.
- Existing encrypted content keeps working untouched, because the backfill makes
  every stored row state what was already true of it.
