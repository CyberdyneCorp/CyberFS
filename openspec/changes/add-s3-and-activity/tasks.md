## 1. Operation auditing (shared prerequisite)

- [x] 1.1 Add the file-operation audit actions (`file.uploaded`, `file.downloaded`, `node.created`, `node.renamed`, `node.moved`, `node.copied`, `node.deleted`, `node.restored`, `version.restored`) to `AuditAction`
- [x] 1.2 Add a `protocol` field to `AuditRecord` and its row, so REST and S3 traffic are distinguishable, defaulting to `rest`
- [x] 1.3 Emit records from `NodeService` and `ContentService` for every operation named above, carrying the actor, node, and plaintext byte count where one applies
- [x] 1.4 Attribute public-link reads to the link rather than to a user, since there is no authenticated caller
- [x] 1.5 Make audit writes non-blocking: a failed record logs at error level and the operation still succeeds
- [x] 1.6 Omit the file name from any record whose actor does not own the node
- [x] 1.7 Add the index on `(actor_subject, occurred_at)` the summary query must be answerable from, with a migration
- [x] 1.8 Unit tests for every scenario in `file-storage/spec.md`'s "File operations are auditable" requirement

## 2. Activity reporting

- [x] 2.1 Add `ACTIVITY_RETENTION_DAYS` and `ACTIVITY_MAX_WINDOW_DAYS` settings, documented in `.env.example`
- [x] 2.2 Define the activity rollup and feed value objects in `domain/activity.py`, pure and independently testable
- [x] 2.3 Implement the aggregate query: counts per action, bytes uploaded and downloaded, and the busiest day, over a bounded window
- [x] 2.4 Implement `ActivityService` returning the rollup plus a paginated, newest-first feed, filterable by action
- [x] 2.5 Expose `GET /api/v1/me/activity`, self-scoped with no parameter naming another subject, refusing a window beyond the maximum with `422`
- [x] 2.6 Identify nodes the caller does not own by id only, never by name
- [x] 2.7 Keep entries for purged nodes in the feed, identified by id
- [x] 2.8 Refuse the endpoint to service principals, which own no tree
- [x] 2.9 Implement the activity prune job, retaining security records (denials, grants, transfers, encryption changes, admin actions) under the longer audit retention
- [x] 2.10 Register the prune job in the admin operations view alongside the existing jobs
- [x] 2.11 Unit tests for every scenario in `activity-reporting/spec.md`
- [x] 2.12 Integration test: a user's uploads, downloads, and shares appear in their own activity and in nobody else's

## 3. S3 access keys

- [x] 3.1 Add the `S3AccessKey` entity and repository port: key id, verifier, label, owner, created, last used, revoked
- [x] 3.2 Write the SQLAlchemy model and migration, with a unique index on the key id and an index on the owner
- [x] 3.3 Implement minting: generate a key id and a high-entropy secret; never store the secret in cleartext -- SigV4 must reproduce it, so seal it under the deployment `MASTER_KEY` (held outside the database), leaving a database leak alone unable to reveal anything; return the secret exactly once
- [x] 3.4 Implement listing (never returning the secret) and revocation with immediate effect
- [x] 3.5 Refuse minting to service principals
- [x] 3.6 Record last-used on every authenticated request, so unused credentials can be retired
- [x] 3.7 Expose `POST`/`GET`/`DELETE` under `/api/v1/me/s3-keys`
- [x] 3.8 Unit tests: the secret is never persisted in cleartext (only sealed), appears in no response after creation, revocation is immediate, and multiple keys coexist for rotation

## 4. Signature V4 verification

- [x] 4.1 Implement canonical request construction (method, URI, query, headers, signed-headers, payload hash) as a pure function
- [x] 4.2 Implement the string-to-sign and signing-key derivation as pure functions
- [x] 4.3 Verify against AWS's own documented SigV4 test vectors, so correctness is measured against the published algorithm rather than our reading of it
- [x] 4.4 Compare signatures in constant time
- [x] 4.5 Enforce `S3_CLOCK_SKEW_SECONDS`, rejecting a stale request with `RequestTimeTooSkewed`
- [x] 4.6 Verify `x-amz-content-sha256` against the received body, so a signature cannot be replayed over altered content
- [x] 4.7 Make an unknown access key and a bad signature indistinguishable by timing
- [x] 4.8 Rate limit repeated signature failures per source IP, reusing the existing limiter
- [x] 4.9 Unit tests for every scenario in `s3-compatibility/spec.md`'s signature requirements, including malformed and truncated headers

## 5. S3 authentication and namespace

- [x] 5.1 Resolve a verified access key to the owning `Principal`, identical to what a bearer token for that user produces
- [x] 5.2 Accept a CyberdyneAuth bearer token on the S3 surface, and refuse a request carrying both credentials with `400`
- [x] 5.3 Strip administrator status from key-authenticated principals and reject key authentication on admin routes
- [x] 5.4 Keep introspection-backed freshness for grants, revocations, and transfers regardless of credential, failing closed on an identity-plane outage
- [x] 5.5 Implement the key-to-node mapping: bucket equals the caller's subject, folder path equals key prefix
- [x] 5.6 Implement the reserved `shared/<owner-subject>/…` prefix for nodes shared with the caller
- [x] 5.7 Reserve the name `shared` at the root of every tree so a real folder cannot shadow the shared view
- [x] 5.8 Answer `NoSuchBucket` for another subject's bucket, identically to a bucket that never existed
- [x] 5.9 Refuse `CreateBucket` and `DeleteBucket`
- [x] 5.10 Unit tests for mapping, the reserved prefix, and cross-user bucket addressing

## 6. S3 read path

- [x] 6.1 Add the `S3_API_ENABLED`, `S3_BASE_PATH`, `S3_REGION`, and `S3_CLOCK_SKEW_SECONDS` settings, documented in `.env.example` (`S3_REGION`/`S3_CLOCK_SKEW_SECONDS` landed in phase 4; phase 6 adds `S3_API_ENABLED`/`S3_BASE_PATH` and mounts the router only when enabled)
- [x] 6.2 Implement S3 XML request and response serialization, including the `<Error>` document shape with code, message, and request id
- [x] 6.3 Implement `ListBuckets`, returning exactly the caller's own bucket
- [x] 6.4 Implement `HeadBucket` and `HeadObject`
- [x] 6.5 Implement `ListObjectsV2` with prefix, delimiter, `CommonPrefixes`, `max-keys`, and continuation tokens, over the caller's authorized view only
- [x] 6.6 Exclude trashed nodes from every listing, and report plaintext sizes for encrypted files
- [x] 6.7 Implement `GetObject`, including `Range` support and decryption of encrypted content
- [x] 6.8 Return `NotImplemented` for unsupported operations, and ignore S3 object-version parameters
- [x] 6.9 Integration test: `boto3` against a running CyberFS lists and downloads a file byte-identically to REST

## 7. S3 write path

- [x] 7.1 Implement `PutObject` over the existing upload use case, so quota, versioning, and encryption inheritance apply unchanged
- [x] 7.2 Return `QuotaExceeded` when an upload would exceed the owner's quota, storing neither object nor metadata
- [x] 7.3 Implement `DeleteObject` and `DeleteObjects` as soft deletes, recoverable for the trash window
- [x] 7.4 Implement `CopyObject` server-side, carrying no grants onto the copy
- [x] 7.5 Refuse writes under `shared/` where the caller holds only `viewer`
- [x] 7.6 Integration test: an upload through S3 is readable through REST and vice versa, including an encrypted file
- [x] 7.7 Integration test: a revoked recipient is refused on the very next S3 request

## 8. Multipart upload

- [x] 8.1 Implement `CreateMultipartUpload`, `UploadPart`, `CompleteMultipartUpload`, `AbortMultipartUpload`, and `ListParts`
- [x] 8.2 Assemble parts in part-number order so the result equals their concatenation
- [x] 8.3 Charge quota on completion, not on each part
- [x] 8.4 Leave no visible node when an upload is aborted, and reclaim its parts
- [x] 8.5 Reclaim uploads abandoned beyond `S3_MULTIPART_ABANDON_HOURS` via the orphan reaper
- [x] 8.6 Integration test: `boto3` (aws-cli is not installed) uploads a file large enough to force multipart, and it round-trips byte-identically

## 9. Presigned URLs

- [x] 9.1 Implement presigned URL generation addressing CyberFS's own S3 endpoint
- [x] 9.2 Verify a presigned request's signature and expiry, refusing an expired one with `AccessDenied`
- [x] 9.3 Make a presigned URL stop working the moment the key that signed it is revoked
- [x] 9.4 Add a test asserting no response from any surface contains the MinIO endpoint, an AWS signature for it, or an object key
- [x] 9.5 Update `file-storage`'s presigned-URL requirement and its tests to the sharpened rule

## 10. Observability and close-out

- [ ] 10.1 Label request metrics by protocol so S3 and REST traffic can be told apart
- [ ] 10.2 Add metrics for signature failures, access-key authentications, and multipart uploads in flight
- [ ] 10.3 Assert no log line carries an access-key secret, a signature, or object content
- [ ] 10.4 Write `docs/s3-api.md`: endpoint, credentials, the bucket and key mapping, the reserved prefix, what is deliberately unsupported and why
- [ ] 10.5 Write `docs/activity.md`: what is recorded, retention, and the privacy boundary
- [ ] 10.6 Update the `bootstrap-cyberfs` design non-goals to record that S3 is now in scope while WebDAV and FUSE remain out
- [ ] 10.7 Resolve this change's open questions — key expiry, anonymous presigned reads, retention numbers, download-record granularity, and bucket naming — recording the decisions
- [ ] 10.8 Run `openspec validate --all --strict` and the full `just ci` gate, then archive the change
