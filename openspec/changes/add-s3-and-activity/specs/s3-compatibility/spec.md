## ADDED Requirements

### Requirement: S3 protocol surface

CyberFS SHALL expose an S3-compatible HTTP surface, rooted at a configurable
path, supporting `ListBuckets`, `ListObjectsV2`, `HeadBucket`, `HeadObject`,
`GetObject`, `PutObject`, `DeleteObject`, `DeleteObjects`, `CopyObject`, and
multipart upload. Unsupported operations SHALL return the S3 error code
`NotImplemented` rather than a CyberFS error shape.

#### Scenario: A standard client can round-trip a file

- **WHEN** a caller uses an unmodified S3 client to `PutObject` and then
  `GetObject` the same key
- **THEN** the bytes returned SHALL equal the bytes sent

#### Scenario: Errors use the S3 XML shape

- **WHEN** any request to the S3 surface fails
- **THEN** the response body SHALL be an S3 `<Error>` document carrying a `Code`,
  a `Message`, and the request id, and SHALL NOT be an RFC 7807 problem document

#### Scenario: An unsupported operation is reported as such

- **WHEN** a client invokes an S3 operation CyberFS does not implement
- **THEN** the system SHALL respond `501` with code `NotImplemented`

#### Scenario: The surface is versionless

- **WHEN** a client issues a request carrying S3 object-version parameters
- **THEN** the system SHALL ignore them and operate on the current version, since
  CyberFS versioning is not S3 object versioning

### Requirement: Signature V4 authentication

CyberFS SHALL authenticate S3 requests signed with AWS Signature Version 4
against a CyberFS-issued access key, and SHALL resolve the key to the
CyberdyneAuth subject that owns it.

#### Scenario: A correctly signed request is accepted

- **WHEN** a request carries a valid `AWS4-HMAC-SHA256` authorization header
  computed with the secret belonging to an active access key
- **THEN** the system SHALL resolve the caller to that key's subject and apply the
  same permission checks as the REST surface

#### Scenario: A bad signature is refused

- **WHEN** the computed signature does not match the supplied one
- **THEN** the system SHALL respond `403` with code `SignatureDoesNotMatch` and
  SHALL NOT disclose whether the access key exists

#### Scenario: An unknown access key is refused indistinguishably

- **WHEN** a request is signed with an access key CyberFS does not hold
- **THEN** the system SHALL respond `403` with code `InvalidAccessKeyId` in
  constant time relative to a bad-signature rejection, so the two cases are not
  distinguishable by timing

#### Scenario: A stale request is refused

- **WHEN** the signed `X-Amz-Date` is more than `S3_CLOCK_SKEW_SECONDS` from the
  server's clock
- **THEN** the system SHALL respond `403` with code `RequestTimeTooSkewed`

#### Scenario: A replayed signature over altered content is refused

- **WHEN** a request's body does not match the signed `x-amz-content-sha256`
- **THEN** the system SHALL reject it

#### Scenario: Signature comparison is constant time

- **WHEN** a supplied signature is compared against the computed one
- **THEN** the comparison SHALL be constant time with respect to the number of
  matching leading bytes

### Requirement: Bearer authentication on the S3 surface

CyberFS SHALL additionally accept a CyberdyneAuth bearer token on the S3
surface, so first-party services can use it without minting an access key.

#### Scenario: A bearer token is accepted

- **WHEN** an S3 request carries `Authorization: Bearer <token>` instead of a
  signature
- **THEN** the system SHALL verify the token exactly as the REST surface does and
  resolve the same principal

#### Scenario: Both credentials on one request is refused

- **WHEN** a request carries both a signature and a bearer token
- **THEN** the system SHALL respond `400`, rather than silently choosing one

#### Scenario: An unauthenticated request is refused

- **WHEN** a request carries neither credential
- **THEN** the system SHALL respond `403` with code `AccessDenied`

#### Scenario: Both paths reach the same authorization

- **WHEN** the same operation is performed by the same subject over a signature
  and over a bearer token
- **THEN** the permission decision SHALL be identical

### Requirement: Access-key lifecycle

CyberFS SHALL let a user mint, list, and revoke S3 access keys scoped to their
own subject. A key SHALL be a credential for an existing identity and SHALL NOT
confer any permission its owner does not already hold.

#### Scenario: A key is minted and shown once

- **WHEN** a user creates an access key
- **THEN** the system SHALL return the access-key id and the secret exactly once,
  and SHALL never persist the secret in cleartext -- sealing it under the
  deployment `MASTER_KEY` held outside the database, so a database leak alone
  recovers nothing

#### Scenario: The secret is never shown again

- **WHEN** a user lists their access keys
- **THEN** each entry SHALL carry the key id, a label, creation time, and
  last-used time, and SHALL NOT carry the secret

#### Scenario: Revocation is immediate

- **WHEN** a user revokes an access key
- **THEN** the next request signed with it SHALL be refused, with no reliance on
  cache expiry

#### Scenario: A key cannot exceed its owner

- **WHEN** a request signed with a user's key targets a node that user may not
  read
- **THEN** the system SHALL refuse it exactly as it would refuse the same user
  over REST

#### Scenario: Keys belong to users, not services

- **WHEN** a service principal attempts to mint an access key
- **THEN** the system SHALL refuse, since a service has no tree of its own

#### Scenario: Last use is recorded

- **WHEN** a request is authenticated with an access key
- **THEN** the system SHALL record the key's last-used time, so an unused
  credential can be identified and retired

#### Scenario: A user may hold several keys

- **WHEN** a user mints a second key while the first is active
- **THEN** both SHALL work, so a credential can be rotated without an outage

### Requirement: Namespace mapping

Each user's tree SHALL appear as a single bucket named for their CyberdyneAuth
subject. Folder paths SHALL map to key prefixes. Nodes shared with the caller
SHALL appear under a reserved `shared/<owner-subject>/…` prefix within that same
bucket.

#### Scenario: The caller sees their own bucket

- **WHEN** a caller issues `ListBuckets`
- **THEN** the response SHALL contain exactly one bucket, named for their subject

#### Scenario: A path maps to a key

- **WHEN** a caller reads the key `reports/q3.xlsx`
- **THEN** the system SHALL resolve it to the node at `/reports/q3.xlsx` in that
  caller's tree

#### Scenario: Shared items are reachable under the reserved prefix

- **WHEN** a node owned by another user has been shared with the caller
- **THEN** it SHALL be listable and readable under
  `shared/<owner-subject>/<path>`

#### Scenario: Another user's bucket is not reachable

- **WHEN** a caller addresses a bucket named for a different subject
- **THEN** the system SHALL respond `404` with code `NoSuchBucket`, so bucket
  names do not become a user directory

#### Scenario: Writes under the shared prefix follow the grant

- **WHEN** a caller writes to a key under `shared/<owner>/…` where they hold only
  `viewer`
- **THEN** the system SHALL respond `403` with code `AccessDenied`

#### Scenario: The reserved prefix cannot be shadowed

- **WHEN** a caller attempts to create a folder named `shared` at the root of
  their own tree
- **THEN** the system SHALL refuse, so a real folder cannot mask the shared view

#### Scenario: Bucket creation and deletion are refused

- **WHEN** a caller issues `CreateBucket` or `DeleteBucket`
- **THEN** the system SHALL respond `403`, since buckets correspond to users and
  are not client-managed

### Requirement: Listing semantics

`ListObjectsV2` SHALL implement prefix and delimiter semantics, pagination by
continuation token, and `CommonPrefixes` for folder-like grouping, over the
caller's authorized view only.

#### Scenario: A delimiter groups folders

- **WHEN** a caller lists with `delimiter=/`
- **THEN** immediate child folders SHALL be returned as `CommonPrefixes` and only
  direct child files as `Contents`

#### Scenario: Listing paginates

- **WHEN** more keys match than `max-keys`
- **THEN** the response SHALL be truncated and carry a continuation token that
  returns the remainder

#### Scenario: Listing shows only what the caller may see

- **WHEN** a caller lists a prefix containing nodes they have no grant on
- **THEN** those nodes SHALL be absent from both `Contents` and `CommonPrefixes`

#### Scenario: Trashed nodes are absent

- **WHEN** a node has been soft-deleted
- **THEN** it SHALL NOT appear in any listing

#### Scenario: Reported sizes are plaintext sizes

- **WHEN** an encrypted file is listed
- **THEN** its reported size SHALL be the plaintext size, matching what a
  `GetObject` returns

### Requirement: Object operations honour the existing model

Every S3 operation SHALL be implemented over the same use cases as the REST
surface, so permission, quota, versioning, encryption, and trash behaviour are
identical by construction rather than by parallel implementation.

#### Scenario: An upload is charged and versioned

- **WHEN** a caller `PutObject`s over an existing key
- **THEN** a new version SHALL be created and the owner's quota charged, exactly
  as an equivalent REST upload

#### Scenario: An upload beyond quota is refused

- **WHEN** a `PutObject` would exceed the owner's quota
- **THEN** the system SHALL respond `403` with code `QuotaExceeded` and store
  neither object nor metadata

#### Scenario: Encryption inheritance applies

- **WHEN** a caller `PutObject`s into a prefix whose folder has an encryption
  default of `on`
- **THEN** the stored content SHALL be encrypted, and a direct read of the
  underlying object SHALL yield no plaintext

#### Scenario: An encrypted object is decrypted on read

- **WHEN** a caller with a wrapped key `GetObject`s an encrypted file
- **THEN** the system SHALL return the plaintext

#### Scenario: A revoked recipient cannot read

- **WHEN** a grant is revoked and the former recipient issues `GetObject` under
  the shared prefix
- **THEN** the system SHALL refuse on the very next request

#### Scenario: Deletion moves to trash

- **WHEN** a caller `DeleteObject`s a key
- **THEN** the node SHALL be soft-deleted and recoverable for the trash retention
  window, not destroyed

#### Scenario: Range reads are supported

- **WHEN** a caller sends a `Range` header on `GetObject`
- **THEN** the system SHALL return `206` with exactly the requested plaintext
  bytes, including for encrypted content

#### Scenario: `CopyObject` duplicates server-side

- **WHEN** a caller copies one key to another
- **THEN** the content SHALL be duplicated without transiting the client, and the
  copy SHALL carry no grants

### Requirement: Multipart upload

CyberFS SHALL support multipart upload so large objects can be written by
standard clients, and SHALL not leave partial uploads occupying storage
indefinitely.

#### Scenario: A multipart upload completes

- **WHEN** a client initiates an upload, sends parts, and completes it
- **THEN** the resulting object SHALL equal the concatenation of the parts in
  part-number order

#### Scenario: An aborted upload leaves no visible file

- **WHEN** a client aborts a multipart upload
- **THEN** no node SHALL become visible and any stored parts SHALL be reclaimed

#### Scenario: An abandoned upload is reclaimed

- **WHEN** a multipart upload is neither completed nor aborted within
  `S3_MULTIPART_ABANDON_HOURS`
- **THEN** its parts SHALL be reclaimed by the orphan reaper

#### Scenario: Quota is charged on completion

- **WHEN** a multipart upload completes
- **THEN** the owner SHALL be charged for the assembled object

### Requirement: Presigned URLs resolve to CyberFS

CyberFS MAY issue presigned URLs, and every such URL SHALL address CyberFS's own
S3 endpoint. CyberFS SHALL NOT issue a URL that grants direct access to the
underlying object store.

#### Scenario: A presigned URL points at CyberFS

- **WHEN** a presigned URL is generated
- **THEN** its host SHALL be the CyberFS S3 endpoint, and using it SHALL cause
  the bytes to transit CyberFS, subject to authorization, quota, and decryption

#### Scenario: The underlying store is never delegated

- **WHEN** any presigned URL is generated
- **THEN** it SHALL NOT reference the MinIO endpoint and SHALL NOT carry a
  credential permitting direct bucket access

#### Scenario: An expired presigned URL is refused

- **WHEN** a presigned URL is used after its expiry
- **THEN** the system SHALL respond `403` with code `AccessDenied`

#### Scenario: A presigned URL cannot exceed its signer

- **WHEN** the key that signed a presigned URL is revoked
- **THEN** the URL SHALL stop working immediately

### Requirement: The S3 surface is observable and audited

S3 operations SHALL emit the same audit records and metrics as their REST
equivalents, distinguished by the protocol used.

#### Scenario: An S3 operation is audited

- **WHEN** a caller uploads, downloads, or deletes over S3
- **THEN** an audit record SHALL be written carrying the subject, the node, and a
  marker identifying the S3 protocol

#### Scenario: Metrics distinguish the surfaces

- **WHEN** metrics are scraped
- **THEN** request counts SHALL be labelled by protocol, so S3 and REST traffic
  can be told apart

#### Scenario: Signature failures are rate limited

- **WHEN** a source IP accumulates repeated signature failures
- **THEN** the system SHALL rate limit it as it does authentication failures on
  the REST surface

#### Scenario: Secrets never reach a log

- **WHEN** any S3 request is logged
- **THEN** the record SHALL NOT contain the access-key secret, the signature, or
  the object's content
