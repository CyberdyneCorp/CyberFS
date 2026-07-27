## Why

Two gaps surfaced once CyberFS had a working REST API.

**Nothing existing speaks to it.** Every tool that already knows how to move
files — `aws-cli`, `rclone`, `boto3`, Cyberduck, backup agents, CI jobs — speaks
S3. Asking each product team to write a CyberFS client is the tax that stops a
storage service from being adopted. An S3-compatible surface over the same tree,
the same permission model, and the same encryption removes that tax entirely.

**Users cannot see what they did.** CyberFS records an audit trail, but it is
admin-only and, more importantly, records almost nothing about ordinary file
work: uploads, downloads, creates, moves, and deletions leave no trace at all. A
user who wants to answer "what happened to my files this month", or who suspects
a share is being used more than expected, has nowhere to look.

## What Changes

- **New `s3-compatibility` capability** — an S3 protocol surface at
  `/s3` covering the object and bucket operations real clients use: `ListBuckets`,
  `ListObjectsV2`, `HeadObject`, `GetObject`, `PutObject`, `DeleteObject`,
  `DeleteObjects`, `CopyObject`, and multipart upload.
- **Dual authentication on that surface** — AWS Signature V4 against
  CyberFS-issued access keys (so standard tooling works unmodified), *and*
  CyberdyneAuth bearer tokens (so first-party services reuse the credential they
  already hold). Both resolve to the same CyberdyneAuth subject and the same
  permission checks.
- **New S3 access-key credentials** — users mint, list, and revoke key pairs
  scoped to their own subject. The secret is shown once. Keys are a credential
  *for* an existing identity, never a second identity.
- **Namespace mapping** — one bucket per user, named for their subject; folder
  paths become key prefixes. Items shared with the caller appear under a reserved
  `shared/<owner-subject>/…` prefix so a recipient reaches them with the same
  credential.
- **New `activity-reporting` capability** — `GET /api/v1/me/activity` returning
  rollup counts over a window (uploads, downloads, shares granted and revoked,
  deletions, bytes moved) plus a paginated chronological feed of the individual
  operations behind those numbers.
- **MODIFIED `file-storage`** — the "no presigned URLs" rule is sharpened. The
  prohibition is on delegating *direct MinIO access*; a CyberFS-issued presigned
  URL that resolves to CyberFS's own S3 endpoint is permitted, because every byte
  still transits the service and remains subject to authorization, quota, and
  decryption. Written explicitly so the two rules cannot read as contradictory.
- **MODIFIED `authentication`** — a second credential type resolving to a
  CyberdyneAuth subject, with the rules that keep it from becoming a bypass.
- **File operations become auditable** — uploads, downloads, creates, moves,
  deletions, and restores emit audit records. This is what the activity feed is
  built from, and it also closes a real gap in the existing admin audit log.

**BREAKING**: none. Both capabilities are additive; existing REST behaviour is
unchanged.

## Capabilities

### New Capabilities

- `s3-compatibility`: the S3 protocol surface — request signing, bucket and
  object semantics, the tree-to-key mapping, multipart upload, and the
  interaction with sharing and encryption.
- `activity-reporting`: per-user operation history — what is recorded, the
  rollup, the feed, retention, and the privacy boundary that keeps one user's
  activity out of another's view.

### Modified Capabilities

- `file-storage`: the presigned-URL prohibition is narrowed to direct MinIO
  access, and file operations gain audit records.
- `authentication`: S3 access keys are added as a credential type that resolves
  to a CyberdyneAuth subject.

## Impact

- **Reverses a documented non-goal.** `design.md` currently lists "WebDAV, FUSE,
  S3-compatible gateway, or any other protocol surface beyond REST" as out of
  scope. That decision is reversed for S3 specifically and the reasoning is
  recorded in this change's `design.md`; WebDAV and FUSE remain non-goals.
- **New attack surface.** An S3 endpoint is unauthenticated until a signature is
  verified, and signature verification is security-critical code. It also
  invites credential sprawl: long-lived access keys are exactly the thing that
  leaks into CI logs and laptops, which is why revocation, listing, and last-used
  tracking are specified rather than left to a later pass.
- **New write path to the same data.** Everything reachable over S3 must enforce
  the identical permission, quota, and encryption rules as REST. The risk is not
  that S3 is wrong on day one but that the two paths drift; the specs therefore
  require the S3 surface to be implemented over the existing use cases rather
  than beside them.
- **Audit volume rises sharply.** Recording every download turns a quiet table
  into the busiest one in the schema, so retention and indexing are specified
  rather than assumed.
- **New dependencies**: none. Signature verification uses the standard library
  and `cryptography`, both already present.
