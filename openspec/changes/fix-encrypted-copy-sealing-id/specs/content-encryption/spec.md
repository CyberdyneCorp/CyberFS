## MODIFIED Requirements

### Requirement: Content sealing

Encrypted content SHALL be sealed with AES-256-GCM in fixed-size frames, each frame carrying a unique nonce and an authentication tag, with the frame index and a version identifier bound as associated data. Every stored version SHALL record the version identifier its bytes were sealed under, and decryption SHALL authenticate against that recorded identifier rather than against the identifier of the version row being served. For content sealed in place the two SHALL be the same value; they differ only where a version's bytes were copied from another version.

#### Scenario: Ciphertext is what MinIO stores

- **WHEN** an encrypted file is uploaded
- **THEN** the bytes written to MinIO SHALL be ciphertext, and reading the object directly SHALL yield no plaintext

#### Scenario: Nonces never repeat under one key

- **WHEN** a file is written
- **THEN** every frame SHALL use a distinct nonce, and a DEK SHALL never be reused across files

#### Scenario: Versions of one file share a key and are separated by associated data

- **WHEN** a new version of an encrypted file is written
- **THEN** it SHALL be sealed under the data key already wrapped for that file rather than under a new one, because the store holds one wrapped key per file and recipient and a replacement key would leave every earlier version unreadable
- **AND** the version identifier bound as associated data SHALL be what separates one version's frames from another's

#### Scenario: Copied content stays readable without being re-encrypted

- **WHEN** a version's bytes are copied to a new version, whether by copying a file or by restoring an earlier version
- **THEN** the new version SHALL record the sealing identifier of the version it was copied from, SHALL be readable as the same plaintext, and the bytes SHALL NOT be decrypted or re-encrypted in order to make that so

#### Scenario: Tampered ciphertext rejected

- **WHEN** a stored frame's bytes are altered
- **THEN** decryption SHALL fail authentication, the download SHALL abort with `500` and error code `integrity_failure`, and an alert-level log SHALL be emitted

#### Scenario: Frame reordering rejected

- **WHEN** two frames of a file are swapped in storage
- **THEN** decryption SHALL fail because the frame index is bound as associated data

#### Scenario: Cross-version frame substitution rejected

- **WHEN** a frame from a version holding different content is substituted
- **THEN** decryption SHALL fail because the sealing identifier is bound as associated data. Two versions that hold identical copied content SHALL share a sealing identifier, and substituting between them is therefore not a substitution of content

#### Scenario: Streaming decryption

- **WHEN** an encrypted file is downloaded
- **THEN** the system SHALL decrypt frame by frame while streaming and SHALL NOT materialize the whole plaintext in memory or on disk

#### Scenario: Range request on encrypted content

- **WHEN** a `Range` request targets an encrypted file
- **THEN** the system SHALL decrypt only the frames covering the requested range and serve exactly the requested plaintext bytes
