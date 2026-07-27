# content-encryption Specification

## Purpose
TBD - created by archiving change bootstrap-cyberfs. Update Purpose after archive.
## Requirements
### Requirement: Encryption is optional and inherited

Content encryption SHALL be opt-in. Every file SHALL carry an immutable-at-creation `encrypted` flag. Every folder SHALL carry an `encryption_default` setting of `inherit`, `on`, or `off`. A file created without an explicit choice SHALL take the nearest ancestor's effective default, and the root folder's effective default SHALL be `off` unless `ENCRYPTION_DEFAULT_ON` is set.

#### Scenario: Unencrypted by default

- **WHEN** a file is uploaded into a folder with no encryption default configured anywhere up the tree and `ENCRYPTION_DEFAULT_ON` is unset
- **THEN** the file SHALL be stored as plaintext and its metadata SHALL report `encrypted: false`

#### Scenario: Folder default applied

- **WHEN** a file is uploaded into a folder whose effective encryption default is `on`, with no per-request override
- **THEN** the file SHALL be stored encrypted

#### Scenario: Per-request override wins

- **WHEN** an upload explicitly requests `encrypted: false` into a folder whose default is `on`
- **THEN** the system SHALL store the file as plaintext, and SHALL record the override in the audit log

#### Scenario: Deployment-wide default

- **WHEN** `ENCRYPTION_DEFAULT_ON` is set and no folder overrides it
- **THEN** every newly created file SHALL be encrypted

#### Scenario: Existing files unaffected by a default change

- **WHEN** a folder's encryption default is changed
- **THEN** files already inside it SHALL retain their existing encryption state

### Requirement: Converting a file's encryption state

Changing an existing file's encryption state SHALL be an explicit operation that rewrites content into a new version, and SHALL never silently downgrade protection.

#### Scenario: Plaintext file encrypted

- **WHEN** the owner requests encryption of an existing plaintext file
- **THEN** the system SHALL create a new encrypted version, mark the file `encrypted: true`, and delete the plaintext objects of all retained versions

#### Scenario: Encrypted file decrypted

- **WHEN** the owner explicitly requests decryption of an encrypted file
- **THEN** the system SHALL require `owner` permission, write plaintext objects, destroy the wrapped data keys, and record the downgrade in the audit log

#### Scenario: Editor cannot change encryption state

- **WHEN** a caller holding `editor` requests a change of encryption state
- **THEN** the system SHALL respond `403 Forbidden`

#### Scenario: Conversion is atomic

- **WHEN** a conversion fails partway
- **THEN** the file SHALL remain fully readable in its original state and no partially written objects SHALL be referenced

### Requirement: Key hierarchy

Encrypted content SHALL be protected by a three-level envelope: a per-file **data key (DEK)**, wrapped by a per-user **key-encryption key (KEK)**, itself wrapped by the deployment `MASTER_KEY`. Unwrapped DEKs and KEKs SHALL exist only in process memory for the duration of a request.

#### Scenario: DEK generated per file

- **WHEN** an encrypted file is created
- **THEN** the system SHALL generate a fresh 256-bit DEK from a cryptographically secure source, unique to that file

#### Scenario: KEK generated per user

- **WHEN** a user record is provisioned
- **THEN** the system SHALL generate a fresh 256-bit KEK, store it wrapped under `MASTER_KEY`, and never store it in the clear

#### Scenario: Plaintext keys never persisted

- **WHEN** any key is written to Postgres, MinIO, Redis, a log line, or an error response
- **THEN** it SHALL be in wrapped form only

#### Scenario: Missing master key fails startup

- **WHEN** the service starts without `MASTER_KEY` while encrypted files exist
- **THEN** it SHALL fail its readiness probe rather than serving requests that would return `500` per file

### Requirement: Content sealing

Encrypted content SHALL be sealed with AES-256-GCM in fixed-size frames, each frame carrying a unique nonce and an authentication tag, with the frame index and the file's version identifier bound as associated data.

#### Scenario: Ciphertext is what MinIO stores

- **WHEN** an encrypted file is uploaded
- **THEN** the bytes written to MinIO SHALL be ciphertext, and reading the object directly SHALL yield no plaintext

#### Scenario: Nonces never repeat under one key

- **WHEN** a file is written
- **THEN** every frame SHALL use a distinct nonce, and a DEK SHALL never be reused across files or across versions of the same file

#### Scenario: Tampered ciphertext rejected

- **WHEN** a stored frame's bytes are altered
- **THEN** decryption SHALL fail authentication, the download SHALL abort with `500` and error code `integrity_failure`, and an alert-level log SHALL be emitted

#### Scenario: Frame reordering rejected

- **WHEN** two frames of a file are swapped in storage
- **THEN** decryption SHALL fail because the frame index is bound as associated data

#### Scenario: Cross-version frame substitution rejected

- **WHEN** a frame from another version of the same file is substituted
- **THEN** decryption SHALL fail because the version identifier is bound as associated data

#### Scenario: Streaming decryption

- **WHEN** an encrypted file is downloaded
- **THEN** the system SHALL decrypt frame by frame while streaming and SHALL NOT materialize the whole plaintext in memory or on disk

#### Scenario: Range request on encrypted content

- **WHEN** a `Range` request targets an encrypted file
- **THEN** the system SHALL decrypt only the frames covering the requested range and serve exactly the requested plaintext bytes

### Requirement: Key access follows sharing

The set of principals who can obtain a file's DEK SHALL be exactly the set of principals authorized to read that file: its owner and the recipients of grants that reach it.

#### Scenario: Wrapped key created on share

- **WHEN** an encrypted file is shared with a user
- **THEN** the system SHALL unwrap the DEK using the sharer's KEK and store a copy wrapped under the recipient's KEK, in the same transaction as the grant

#### Scenario: Folder share rewraps descendants

- **WHEN** a folder containing encrypted files is shared
- **THEN** the system SHALL rewrap the DEK of every encrypted descendant for the recipient, and SHALL rewrap for encrypted files added later at creation time

#### Scenario: Large folder share rewraps in the background

- **WHEN** a folder with more encrypted descendants than `ASYNC_REWRAP_THRESHOLD_NODES` is shared
- **THEN** the system SHALL defer the rewrap to a background worker that rewraps every encrypted descendant idempotently and grants access only on completion, so an interrupted worker leaves the grant unusable and never leaves a DEK wrapped for a principal not yet authorized

#### Scenario: Wrapped key destroyed on revoke

- **WHEN** a grant is revoked
- **THEN** the recipient's wrapped copies of the affected DEKs SHALL be deleted in the same transaction

#### Scenario: Content is never decrypted to storage for sharing

- **WHEN** any share operation occurs
- **THEN** the content objects SHALL remain untouched and only key material SHALL be rewrapped

#### Scenario: Revoked recipient cannot decrypt

- **WHEN** a revoked recipient replays a previously captured request
- **THEN** no wrapped DEK SHALL exist for them and the request SHALL be denied

### Requirement: Administrators cannot read content

No administrative privilege SHALL yield file plaintext. `is_admin` SHALL grant access to metadata and aggregate statistics only.

#### Scenario: Admin download denied

- **WHEN** an administrator requests the content of a file they do not own and have not been granted
- **THEN** the system SHALL respond `403 Forbidden`, whether the file is encrypted or not

#### Scenario: Admin cannot self-grant

- **WHEN** an administrator attempts to create a grant on a node they do not own
- **THEN** the system SHALL respond `403 Forbidden`

#### Scenario: No administrative key escrow

- **WHEN** an administrator uses any admin endpoint
- **THEN** no response SHALL contain a DEK, a KEK, or `MASTER_KEY`, in wrapped or unwrapped form

#### Scenario: Admin surface exposes only metadata

- **WHEN** an administrator inspects a user's storage
- **THEN** the response SHALL contain sizes, counts, types, and timestamps, and SHALL NOT contain file names' content, previews, or bytes

### Requirement: Key rotation

CyberFS SHALL support rotating `MASTER_KEY` and rotating an individual user's KEK without rewriting content objects.

#### Scenario: Master key rotated

- **WHEN** a new `MASTER_KEY` is supplied alongside the previous one and rotation is run
- **THEN** every wrapped KEK SHALL be rewrapped under the new key, content objects SHALL be untouched, and the service SHALL remain readable throughout

#### Scenario: Both keys accepted during rotation

- **WHEN** rotation is in progress
- **THEN** the system SHALL unwrap using either the previous or the new master key and SHALL wrap new material only under the new key

#### Scenario: User key rotated

- **WHEN** a user's KEK is rotated
- **THEN** every DEK wrapped under the old KEK SHALL be rewrapped under the new one, and the old KEK SHALL be destroyed on completion

#### Scenario: Rotation is resumable

- **WHEN** a rotation is interrupted
- **THEN** rerunning it SHALL complete the remaining items without corrupting already-rotated ones

### Requirement: Crypto operations are auditable and non-leaking

CyberFS SHALL audit encryption-state changes, rewraps, and rotations, and SHALL NOT leak key material or plaintext through logs, metrics, traces, or error responses.

#### Scenario: Downgrade audited

- **WHEN** a file is converted from encrypted to plaintext
- **THEN** an audit record SHALL identify the actor, the file, and the time

#### Scenario: Errors carry no key material

- **WHEN** a decryption failure produces an error response
- **THEN** the response SHALL contain a generic error code and SHALL NOT include nonces, tags, key bytes, or ciphertext fragments

#### Scenario: Cache holds no plaintext content

- **WHEN** any value is written to Redis
- **THEN** it SHALL NOT contain file plaintext or unwrapped key material

### Requirement: Tags and metadata are outside the encryption boundary

Node tags and key/value metadata SHALL be stored in plaintext, because they are indexed so they can be searched. This is a recorded consequence of the feature, not an oversight.

#### Scenario: Labels are readable from the database

- **WHEN** tags or metadata are stored
- **THEN** they SHALL be stored in plaintext and SHALL be indexable, and the documentation SHALL state that anything placed in them is readable by whoever can read the database

#### Scenario: Content encryption is unaffected

- **WHEN** a node carries tags or metadata
- **THEN** its content SHALL remain encrypted on the same terms as any other node, and no tag or metadata value SHALL be derived from the content

