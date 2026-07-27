# file-storage Specification

## Purpose
TBD - created by archiving change bootstrap-cyberfs. Update Purpose after archive.
## Requirements
### Requirement: Hierarchical namespace

CyberFS SHALL model storage as a tree of nodes, where every node is either a folder or a file, every node except a user root has exactly one parent folder, and every user has exactly one root folder created with their local record.

#### Scenario: Root folder created with the user

- **WHEN** a user record is provisioned
- **THEN** the system SHALL create exactly one root folder owned by that user, with no parent and a name that cannot be changed or deleted

#### Scenario: Node addressed by identifier

- **WHEN** a client requests a node by its opaque identifier
- **THEN** the system SHALL resolve it without requiring the client to know its path

#### Scenario: Path derived, not stored as truth

- **WHEN** a folder is renamed
- **THEN** the reported paths of all its descendants SHALL reflect the new name without any descendant row being rewritten

#### Scenario: Cycles rejected

- **WHEN** a move would make a folder its own ancestor or descendant
- **THEN** the system SHALL respond `409 Conflict` with error code `would_create_cycle` and SHALL make no change

### Requirement: Name validity and uniqueness

Node names SHALL be 1–255 UTF-8 characters, SHALL NOT contain `/`, `\`, or NUL, SHALL NOT be `.` or `..`, and SHALL be unique among the non-deleted children of the same parent, compared case-sensitively after Unicode NFC normalization.

#### Scenario: Duplicate name rejected

- **WHEN** a client creates a node whose name matches a non-deleted sibling
- **THEN** the system SHALL respond `409 Conflict` with error code `name_taken`

#### Scenario: Name reusable after deletion

- **WHEN** a node is soft-deleted and a new node is created in the same parent with the same name
- **THEN** the system SHALL accept the creation

#### Scenario: Path separator rejected

- **WHEN** a client supplies a name containing `/`
- **THEN** the system SHALL respond `422 Unprocessable Entity` and SHALL NOT create the node

#### Scenario: Names normalized consistently

- **WHEN** two names differ only by Unicode normalization form
- **THEN** the system SHALL treat them as the same name for uniqueness purposes

### Requirement: Folder CRUD

CyberFS SHALL allow an authorized caller to create, list, rename, move, and delete folders.

#### Scenario: Folder created

- **WHEN** a caller with `editor` or `owner` permission on the parent creates a folder
- **THEN** the system SHALL create it and respond `201 Created` with its identifier, name, parent, and timestamps

#### Scenario: Folder listed with pagination

- **WHEN** a caller with at least `viewer` permission lists a folder's children
- **THEN** the system SHALL return only children the caller may see, ordered deterministically, with a cursor for pages beyond `PAGE_SIZE_DEFAULT` entries

#### Scenario: Folder deleted recursively

- **WHEN** a caller with `owner` permission deletes a non-empty folder
- **THEN** the system SHALL soft-delete the folder and every descendant in a single transaction

#### Scenario: Delete without permission rejected

- **WHEN** a caller with only `editor` permission attempts to delete a folder
- **THEN** the system SHALL respond `403 Forbidden`

### Requirement: File upload

CyberFS SHALL accept file content as a streamed request body, SHALL write it to MinIO in chunks without buffering the whole object in memory, and SHALL create the metadata record only after the object write completes successfully.

#### Scenario: File uploaded

- **WHEN** a caller with `editor` or `owner` permission on the parent uploads content
- **THEN** the system SHALL store the bytes in MinIO, record size, content type, and a SHA-256 digest of the plaintext, and respond `201 Created`

#### Scenario: Upload streams without full buffering

- **WHEN** an upload of `MAX_UPLOAD_BYTES` is in progress
- **THEN** the service SHALL hold no more than `UPLOAD_CHUNK_BYTES` of that body in memory at any instant

#### Scenario: Interrupted upload leaves no visible file

- **WHEN** the connection drops before the object write completes
- **THEN** the system SHALL NOT create a metadata record, and the partial object SHALL be removed by the orphan reaper

#### Scenario: Oversized upload rejected

- **WHEN** an upload exceeds `MAX_UPLOAD_BYTES`
- **THEN** the system SHALL abort the transfer, respond `413 Payload Too Large`, and store no metadata record

#### Scenario: Declared length mismatch rejected

- **WHEN** the received body length differs from a supplied `Content-Length`
- **THEN** the system SHALL discard the object and respond `400 Bad Request`

### Requirement: File download

CyberFS SHALL stream file content back through the API, and SHALL NOT issue any
URL or credential that would let a client read an object directly from the
underlying object store.

A presigned URL that resolves to CyberFS's *own* S3 endpoint is permitted: the
bytes still transit CyberFS and remain subject to authorization, quota
accounting, and decryption. What is prohibited is delegation of direct MinIO
access, because that would bypass every one of those checks. The distinction is
between a URL CyberFS honours and a URL that hands the object store away.

#### Scenario: File downloaded

- **WHEN** a caller with at least `viewer` permission requests a file's content
- **THEN** the system SHALL stream the plaintext bytes with the recorded content type and a `Content-Length` equal to the plaintext size

#### Scenario: Range request served

- **WHEN** a caller sends a `Range: bytes=<start>-<end>` header for an unencrypted file
- **THEN** the system SHALL respond `206 Partial Content` with exactly the requested plaintext byte range

#### Scenario: Direct object access is never delegated

- **WHEN** any download is served
- **THEN** the response SHALL NOT contain a presigned MinIO URL or any credential permitting direct bucket access

#### Scenario: A CyberFS-issued presigned URL is permitted

- **WHEN** a presigned URL is issued for the S3-compatible surface
- **THEN** it SHALL address CyberFS's own endpoint, and following it SHALL cause
  the bytes to transit CyberFS subject to the usual permission, quota, and
  decryption handling

#### Scenario: The object store endpoint never appears in a response

- **WHEN** any response is produced by any surface
- **THEN** it SHALL NOT contain the configured MinIO endpoint, an AWS signature
  for it, or an object key

#### Scenario: Download denied without permission

- **WHEN** a caller with no grant on the file requests its content
- **THEN** the system SHALL respond `404 Not Found` so that existence is not disclosed

### Requirement: File versioning

CyberFS SHALL retain prior content versions of a file up to `VERSION_RETENTION_COUNT`, SHALL expose them for listing and restore, and SHALL count every retained version against the owner's quota.

#### Scenario: New version created on content update

- **WHEN** a caller with `editor` permission replaces a file's content
- **THEN** the system SHALL create a new version, keep the previous version's object, and make the new version current

#### Scenario: Version restored

- **WHEN** a caller with `editor` permission restores an earlier version
- **THEN** the system SHALL create a new current version whose content equals the restored version rather than deleting history

#### Scenario: Oldest versions pruned

- **WHEN** retaining a new version would exceed `VERSION_RETENTION_COUNT`
- **THEN** the system SHALL delete the oldest version's object and metadata and release its quota

#### Scenario: Metadata edit does not create a version

- **WHEN** only a file's name or parent changes
- **THEN** the system SHALL NOT create a new content version

### Requirement: Move, rename, and copy

CyberFS SHALL support moving a node to another folder, renaming it, and copying a file or a folder subtree.

#### Scenario: Node moved

- **WHEN** a caller with `editor` permission on both source and destination parents moves a node
- **THEN** the system SHALL reparent it, leaving content objects untouched

#### Scenario: Move across owners rejected

- **WHEN** a move would place a node under a folder owned by a different user
- **THEN** the system SHALL respond `409 Conflict` with error code `cross_owner_move` unless ownership transfer was explicitly requested

#### Scenario: Copy duplicates content

- **WHEN** a caller with `viewer` permission on the source and `editor` permission on the destination copies a file
- **THEN** the system SHALL create an independent node owned by the caller, with fresh content objects, counted against the caller's quota

#### Scenario: Copy does not carry grants

- **WHEN** a shared node is copied
- **THEN** the copy SHALL have no share grants and SHALL be visible only to its new owner

### Requirement: Soft delete, restore, and purge

Deletion SHALL move a node into a recoverable state for `TRASH_RETENTION_DAYS`, after which it SHALL be purged permanently. Purge SHALL delete both the metadata and the underlying objects. Purge SHALL also be available on demand for a node already in the trash, releasing its quota immediately.

#### Scenario: Deleted node hidden from listings

- **WHEN** a node is soft-deleted
- **THEN** the system SHALL exclude it from folder listings and search while retaining it in the owner's trash view

#### Scenario: Restore returns node to its parent

- **WHEN** the owner restores a soft-deleted node whose parent still exists and is not deleted
- **THEN** the system SHALL make it visible again in that parent

#### Scenario: Restore lifts the subtree the delete trashed

- **WHEN** the owner restores a soft-deleted folder
- **THEN** the system SHALL bring back every descendant that the same delete trashed, SHALL advance the revision of each node it brings back so a precondition taken before the delete no longer matches, and SHALL return exactly those nodes' bytes from the trashed bucket to the live one

#### Scenario: A descendant deleted on its own occasion stays trashed

- **WHEN** the owner restores a folder holding a descendant that was soft-deleted separately beforehand
- **THEN** the system SHALL leave that descendant in the trash with its bytes still counted as trashed, counted once

#### Scenario: Deleting charges only what it moved

- **WHEN** the owner soft-deletes a folder holding a descendant that was already in the trash
- **THEN** the system SHALL move only the bytes it actually trashed, leaving the already-trashed descendant counted once rather than twice

#### Scenario: Restore to a deleted parent

- **WHEN** the owner restores a node whose original parent has been purged
- **THEN** the system SHALL restore it into the owner's root folder

#### Scenario: Retention expiry purges content

- **WHEN** a node has been soft-deleted for longer than `TRASH_RETENTION_DAYS`
- **THEN** the purge job SHALL delete its metadata, all version objects, and all associated wrapped keys, and SHALL release the quota

#### Scenario: Grants revoked on delete

- **WHEN** a shared node is soft-deleted
- **THEN** recipients SHALL immediately lose access, and the grants SHALL be removed on purge

#### Scenario: On-demand purge destroys a trashed node

- **WHEN** the owner purges a node that is in the trash
- **THEN** the system SHALL delete its metadata, every version's stored object, its wrapped data keys, its grants, and its public links, and SHALL release the quota immediately

#### Scenario: A live node cannot be purged

- **WHEN** a purge is requested for a node that has not been soft-deleted
- **THEN** the system SHALL refuse with `409` and SHALL destroy nothing

#### Scenario: Purging a folder purges its subtree

- **WHEN** the owner purges a trashed folder that contains descendants
- **THEN** the system SHALL delete the stored objects of every descendant as well as the folder, leaving no object that no metadata references

#### Scenario: An administrator may purge another user's trashed node

- **WHEN** an administrator purges a trashed node they do not own
- **THEN** the system SHALL perform the purge and SHALL release the owner's quota, not the administrator's

#### Scenario: A non-owner without administrator rights may not purge

- **WHEN** a caller who is neither the owner nor an administrator requests a purge, including a recipient holding a share of that node
- **THEN** the system SHALL refuse with `404`, indistinguishably from a node that does not exist, and SHALL destroy nothing

#### Scenario: Purge is attributable after activity records age out

- **WHEN** a node is purged
- **THEN** the system SHALL record it as a security record, which SHALL be retained rather than pruned with activity records

#### Scenario: Purging an unknown node

- **WHEN** a purge names a node that does not exist, or one already purged
- **THEN** the system SHALL respond `404` rather than reporting success

### Requirement: Object layout and integrity

Object keys in MinIO SHALL be derived from opaque identifiers and SHALL NOT encode user-supplied names or paths. Every stored object SHALL be verifiable against a recorded digest.

#### Scenario: Key contains no user-controlled text

- **WHEN** a file named `../../etc/passwd` is uploaded
- **THEN** the resulting object key SHALL contain only the owner id, node id, and version id

#### Scenario: Digest mismatch surfaced

- **WHEN** a download's computed plaintext digest differs from the recorded digest
- **THEN** the system SHALL abort the response, respond `500 Internal Server Error` with error code `integrity_failure`, and emit an alert-level log

#### Scenario: Orphaned objects reaped

- **WHEN** the reaper finds an object older than `ORPHAN_GRACE_MINUTES` with no referencing metadata row
- **THEN** it SHALL delete that object and record the reclaimed bytes

### Requirement: Storage quotas

Each user SHALL have a storage quota, defaulting to `DEFAULT_QUOTA_BYTES` and adjustable by an administrator. Quota SHALL be charged to the node owner, count all retained versions and trashed nodes, and SHALL NOT be charged again to share recipients.

#### Scenario: Upload exceeding quota rejected

- **WHEN** an upload would push the owner's usage above their quota
- **THEN** the system SHALL abort it, respond `507 Insufficient Storage`, and store neither object nor metadata

#### Scenario: Trash counts against quota

- **WHEN** a user soft-deletes a file
- **THEN** their reported usage SHALL remain unchanged until the node is purged

#### Scenario: Recipient not charged

- **WHEN** a file is shared with another user
- **THEN** the file's bytes SHALL count only against the owner's quota

#### Scenario: Usage reconciled

- **WHEN** the reconciliation job runs
- **THEN** it SHALL recompute each user's usage from metadata and correct any drift in the cached counter

### Requirement: Listing, search, and metadata

CyberFS SHALL expose node metadata — identifier, name, type, parent, owner, size, content type, digest, encryption state, timestamps, tags, key/value metadata, and the caller's effective permission — and SHALL allow searching by name, tag, and metadata within the subtrees a caller may access.

#### Scenario: Effective permission reported

- **WHEN** a caller reads a node's metadata
- **THEN** the response SHALL include the caller's effective permission for that node

#### Scenario: Search scoped to accessible nodes

- **WHEN** a caller searches by name substring
- **THEN** results SHALL include only nodes the caller owns or has been granted, and SHALL never include nodes from other users' private subtrees

#### Scenario: Content is not searchable

- **WHEN** a caller searches
- **THEN** the system SHALL match on metadata only and SHALL NOT index or match file content

#### Scenario: The content digest is reported

- **WHEN** a caller who may read a file reads its metadata or lists its versions
- **THEN** the response SHALL carry the SHA-256 digest of the plaintext for the current version and for each listed version

#### Scenario: The digest is withheld from the administrative surface

- **WHEN** an administrator views any administrative report
- **THEN** no digest SHALL appear, because a plaintext digest would let a holder test whether a user has a specific known file even though its content is encrypted

### Requirement: Concurrency safety

Concurrent mutations of the same node SHALL NOT corrupt the tree or lose content, and clients SHALL be able to detect lost updates.

#### Scenario: Concurrent create of the same name

- **WHEN** two requests concurrently create a child with the same name in the same parent
- **THEN** exactly one SHALL succeed and the other SHALL receive `409 Conflict`

#### Scenario: Stale update rejected

- **WHEN** a caller supplies an `If-Match` version token that no longer matches the node's current token
- **THEN** the system SHALL respond `412 Precondition Failed` and SHALL make no change

#### Scenario: Concurrent moves serialized

- **WHEN** two moves that would jointly create a cycle are attempted concurrently
- **THEN** the system SHALL serialize them so that at most one succeeds and the tree remains acyclic

### Requirement: File operations are auditable

Every operation that creates, reads, alters, or removes a node SHALL emit an
audit record identifying the actor, the node, the operation, and the protocol
through which it arrived.

#### Scenario: Every mutating operation is recorded

- **WHEN** a node is created, renamed, moved, copied, deleted, or restored
- **THEN** an audit record SHALL be written naming the actor and the node

#### Scenario: Reads are recorded

- **WHEN** file content is downloaded
- **THEN** an audit record SHALL be written, so an owner can establish that their
  content was read and when

#### Scenario: The protocol is distinguishable

- **WHEN** an operation arrives over the S3 surface rather than REST
- **THEN** the record SHALL identify which, so traffic can be attributed

#### Scenario: Audit failure does not fail the operation

- **WHEN** writing the audit record fails
- **THEN** the operation SHALL still succeed and the failure SHALL be logged at
  error level

### Requirement: Tags

A node SHALL carry a set of short labels, independent of its name, that a caller may filter on.

#### Scenario: Tags are set and read back

- **WHEN** a caller with `EDITOR` replaces a node's tags
- **THEN** the node SHALL carry exactly those tags, and a caller with `VIEWER` SHALL see them

#### Scenario: Tags are a set, not a list

- **WHEN** tags are written with duplicates, or in a different order
- **THEN** the stored result SHALL be the same set, with each tag present once

#### Scenario: Tags are normalized

- **WHEN** a tag is written with differing case or surrounding whitespace
- **THEN** it SHALL be normalized so that searching for it matches regardless of how it was typed

#### Scenario: A tag that is empty or too long is refused

- **WHEN** a tag is blank, whitespace only, or longer than the permitted length
- **THEN** the system SHALL refuse the request and SHALL change nothing

#### Scenario: Too many tags are refused

- **WHEN** a write would leave a node with more tags than the permitted maximum
- **THEN** the system SHALL refuse the request and SHALL change nothing

#### Scenario: Writing tags requires edit permission

- **WHEN** a caller holding only `VIEWER` attempts to change tags
- **THEN** the system SHALL refuse and SHALL change nothing

#### Scenario: Changing tags is a node mutation

- **WHEN** a node's tags change
- **THEN** its revision SHALL advance, so a stale `If-Match` is refused, and the change SHALL be recorded in the caller's activity

#### Scenario: Tags do not survive the node

- **WHEN** a node is purged
- **THEN** its tags SHALL be removed with it

### Requirement: Key/value metadata

A node SHALL carry key/value string pairs, so an integration can record facts about it that are not expressible as a name or a tag.

#### Scenario: Metadata is set and read back

- **WHEN** a caller with `EDITOR` replaces a node's metadata
- **THEN** the node SHALL carry exactly those pairs, and a caller with `VIEWER` SHALL see them

#### Scenario: A key appears once

- **WHEN** metadata is written containing the same key twice
- **THEN** the system SHALL refuse the request rather than silently keeping one of the values

#### Scenario: Oversized keys or values are refused

- **WHEN** a key is empty or over the permitted length, or a value is over the permitted length
- **THEN** the system SHALL refuse the request and SHALL change nothing

#### Scenario: Too many pairs are refused

- **WHEN** a write would leave a node with more pairs than the permitted maximum
- **THEN** the system SHALL refuse the request and SHALL change nothing

#### Scenario: The reserved namespace is protected

- **WHEN** a caller writes a key in the namespace reserved for system use
- **THEN** the system SHALL refuse the request, so system-written metadata can never be forged by a caller

#### Scenario: Writing metadata requires edit permission

- **WHEN** a caller holding only `VIEWER` attempts to change metadata
- **THEN** the system SHALL refuse and SHALL change nothing

#### Scenario: Metadata does not survive the node

- **WHEN** a node is purged
- **THEN** its metadata SHALL be removed with it

### Requirement: Searching by tag and metadata

Search SHALL accept tag and metadata filters alongside the name substring, under the same access scoping as the name search.

#### Scenario: Search by tag

- **WHEN** a caller searches with a tag filter
- **THEN** results SHALL be the accessible nodes carrying that tag

#### Scenario: Several tags narrow the result

- **WHEN** a caller searches with more than one tag
- **THEN** results SHALL include only nodes carrying every one of them

#### Scenario: Search by metadata key

- **WHEN** a caller searches with a metadata key and no value
- **THEN** results SHALL be the accessible nodes carrying that key, whatever its value

#### Scenario: Search by metadata key and value

- **WHEN** a caller searches with both a key and a value
- **THEN** results SHALL be the accessible nodes where that key holds exactly that value

#### Scenario: Filters combine

- **WHEN** a caller supplies a name substring together with tag or metadata filters
- **THEN** results SHALL satisfy all of them, each filter narrowing the result rather than widening it

#### Scenario: Tag and metadata search obeys the access scope

- **WHEN** a caller searches by tag or metadata
- **THEN** results SHALL include only nodes the caller owns or has been granted, on the same terms as the name search, and a node shared with nobody SHALL never appear for another caller

#### Scenario: A trashed node is not found

- **WHEN** a caller searches by name, tag, or metadata
- **THEN** trashed nodes SHALL NOT appear

#### Scenario: Results stay bounded

- **WHEN** a search matches more nodes than the permitted page size
- **THEN** the system SHALL return at most that many rather than scanning without limit

