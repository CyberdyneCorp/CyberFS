# Tasks

## 1. Domain

- [x] 1.1 Add `seal_version_id: uuid.UUID` to `FileVersion` in `domain/nodes.py`, with a comment saying it is the identifier the bytes were sealed under and that it equals `id` for content sealed in place. No default: a default would let a copy path forget to carry it and silently seal the bug back in
- [x] 1.2 Leave `object_key` alone. Where the bytes live is still derived from the row's own `id`; only what opens them comes from `seal_version_id`

## 2. Persistence

- [x] 2.1 Add `seal_version_id` to `FileVersionRow` in `models.py` as a non-null UUID, with a comment recording why it carries **no** foreign key: it routinely points at a version of another node and must survive that version being pruned by `VERSION_RETENTION_COUNT`, so `CASCADE` would delete a healthy copy and `RESTRICT` would block a legitimate prune
- [x] 2.2 Carry it in both directions in `mappers.py` (`version_to_row`, `version_from_row`)
- [x] 2.3 Write the Alembic migration on top of `b7e3c9a1d2f5`: add the column nullable, `UPDATE file_versions SET seal_version_id = id`, then set `NOT NULL`. A `server_default` cannot reference another column, which is why it is three statements rather than one
- [x] 2.4 State in the migration's docstring that the backfill is exact rather than approximate — every existing row was sealed in place, and rows produced by the broken copy paths were already unreadable and cannot be repaired, since the source identifier was never recorded
- [ ] 2.5 Implement `downgrade` as a plain column drop, and say in the docstring that it returns the database to a schema in which copies of encrypted content are unreadable. Verify `alembic downgrade` then `upgrade` runs. **Written, not executed:** no database in this environment, so the round trip is unproven -- the same gap item 4 of `docs/outstanding-verification.md` records for all seven earlier migrations

## 3. Encryption and content

- [x] 3.1 `EncryptionService.open` is already given an identifier by its caller; change the **call sites** in `application/content.py` to pass `version.seal_version_id` rather than `version.id`. Both of them — the whole-object read and the range read
- [x] 3.2 Every in-place seal sets `seal_version_id` to the new row's own id: first upload, content replacement, and the encryption-state conversion paths
- [x] 3.3 `restore_version` sets `seal_version_id` to the **source version's** `seal_version_id`, not to the source's `id`. Taking the source's own id would break a copy of a copy; taking its sealing id makes the transitive case fall out with no chain to walk
- [x] 3.4 `_copy_content` does the same for `POST /nodes/{id}/copy`
- [x] 3.5 Do not serialize `seal_version_id` in any response schema. It is key-material metadata, and no client has a use for it

- [x] 3.6 `duplicate` also copies the wrapped data key to the copy's node id. Found while writing 5.2: the copy path recorded no key at all, so an encrypted copy had ciphertext, a version row, and nothing that could open it -- `data_key_for` looks up `(node_id, subject)` and the copy's node id had none

## 4. Unit tests

- [x] 4.1 A `FileVersion` sealed in place reports `seal_version_id == id`
- [x] 4.2 `restore_version` produces a version whose `seal_version_id` is the source's, and whose `id` is its own — asserted against the fake, which is enough because the value is chosen in the application layer
- [x] 4.3 The same for a copy
- [x] 4.4 Restoring a version that was itself a copy carries the original sealing id through, so a copy of a copy is readable
- [x] 4.5 The read path authenticates against `seal_version_id`, pinned with the **real** `AesGcmContentCipher` in `test_encryption_service.py` rather than a recording fake: a restore and a copy of an encrypted file are downloaded and compared byte for byte, which fails the AEAD tag if the wrong identifier is passed. Stronger than asserting the argument, since it proves decryption actually succeeds

## 5. Integration tests (real Postgres and MinIO)

- [ ] 5.1 Restore an earlier version of an **encrypted** file and read it back byte for byte. This is the regression test for the reported defect; it currently sits in `tests/integration/test_api_content.py` as `xfail(strict=True)` and the marker comes off here
- [ ] 5.2 Copy an **encrypted** file and read the copy back byte for byte
- [ ] 5.3 Copy an encrypted file, then restore a version of the copy — the transitive case, which no plaintext test exercises
- [ ] 5.4 The copy's stored object is still ciphertext, so the fix did not quietly stop encrypting on the copy path
- [ ] 5.5 A restored version's digest matches the original's, since the plaintext is the same content
- [ ] 5.6 Confirm the copy path issues no decrypt: assert the copy is byte-identical **in the object store** to its source, which re-encryption under a new nonce could not produce

## 6. Verification

- [x] 6.1 `just lint`, `just typecheck`, `just test-unit` clean
- [ ] 6.2 `just test-integration` clean, verified from the CI run rather than assumed
- [ ] 6.3 `alembic upgrade head` on a database holding encrypted content, then read that content back — the backfill has to leave existing files readable
- [x] 6.4 Remove item 8 from `docs/outstanding-verification.md`
- [x] 6.5 `openspec validate fix-encrypted-copy-sealing-id --strict`
