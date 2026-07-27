## MODIFIED Requirements

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

## ADDED Requirements

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
