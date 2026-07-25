# CyberFS S3-compatible API

CyberFS exposes an S3-compatible HTTP surface so that standard S3 clients
(`boto3`, the AWS SDKs, MinIO clients) can read and write the same trees the
REST API serves. Every operation is delegated to the same
`ContentService`/`NodeService` the REST surface uses, so permission, quota,
versioning, encryption, and trash behaviour are identical by construction — the
S3 surface is a protocol adapter, not a second storage engine
(`adapters/inbound/api/routers/s3.py`).

The authoritative behaviour is the code and the `s3-compatibility/spec.md`
requirements; where this page and the spec disagree, the spec is correct.

## Enabling the surface

The S3 router is mounted only when it is turned on. A deployment that does not
offer S3 exposes no such routes at all.

| Setting | Env var | Default | Meaning |
|---------|---------|---------|---------|
| `s3_api_enabled` | `S3_API_ENABLED` | `false` | Mount the S3 surface. When false, none of the routes below exist. |
| `s3_base_path` | `S3_BASE_PATH` | `/s3` | The path the surface is rooted at. Point an S3 client's endpoint URL here; buckets and keys hang off it. |
| `s3_region` | `S3_REGION` | `us-east-1` | The AWS region a SigV4 credential scope must name. It is part of the signing contract, not a routing hint — clients must sign with this region. |
| `s3_clock_skew_seconds` | `S3_CLOCK_SKEW_SECONDS` | `300` | How far a signed `x-amz-date` may sit from the server clock before the request is refused with `RequestTimeTooSkewed`. |
| `s3_multipart_abandon_hours` | `S3_MULTIPART_ABANDON_HOURS` | `24` | How long a multipart upload may sit neither completed nor aborted before the orphan reaper reclaims its staged parts. |
| `s3_public_endpoint` | `S3_PUBLIC_ENDPOINT` | *(unset)* | The externally visible CyberFS S3 base URL (scheme + host, e.g. `https://s3.cyberfs.example`). Presigned URLs and the multipart `<Location>` are rooted here so they address CyberFS's own endpoint, never the underlying object store. When unset the multipart `<Location>` falls back to the request host; issuing a presigned URL requires it. |

Definitions live in `infrastructure/settings.py`; the router is built by
`create_s3_router(base_path)` in `adapters/inbound/api/routers/s3.py`.

## Credentials

A request to the S3 surface authenticates one of two ways. The credential is
resolved by `S3Authenticator.authenticate` in
`application/s3_authentication.py`, which delegates to the phase-4
`S3SignatureVerifier` (`application/s3_auth.py`) and the REST
`AuthenticationService`.

### SigV4 access keys

The native S3 credential is an access key: a key id and a secret used to sign
each request with AWS Signature Version 4. Keys are minted, listed, and revoked
under the caller's own surface at `/api/v1/me/s3-keys`
(`adapters/inbound/api/routers/s3_keys.py`):

- `POST /api/v1/me/s3-keys` — mint a key. The body carries an optional `label`.
  The response returns the key id **and its secret exactly once**; the secret
  never appears in any later response. The secret is not stored in cleartext —
  it is sealed under the deployment `MASTER_KEY` so a database leak alone
  reveals nothing.
- `GET /api/v1/me/s3-keys` — list your keys. The secret is never included.
- `DELETE /api/v1/me/s3-keys/{key_id}` — revoke a key, with immediate effect. A
  key belonging to another subject is `404`.

The owning subject is always taken from the authenticated principal, never from
a path or body parameter, so no request can name another user's credentials.
Service principals own no tree and are refused minting.

A key resolves to its owner's subject with **administrator status stripped by
construction** — a long-lived key is the credential most likely to leak, so key
authentication can never wield admin and is rejected on admin routes
(`_principal_from_key`).

### Bearer token

The S3 surface also accepts a CyberdyneAuth bearer token
(`Authorization: Bearer <token>`), resolving to exactly the principal the REST
surface would produce, org claims and all, through the identical freshness path.

A request carrying **both** a signature and a bearer token is refused with `400`
(`AmbiguousS3CredentialsError`) — the server never silently picks one. A request
carrying **neither** is `403 AccessDenied`.

## Bucket and key mapping

The namespace grammar is defined purely in `domain/s3/namespace.py`.

- **A bucket is a subject.** Each user's tree is presented as a single bucket
  named for their CyberdyneAuth subject (`bucket_for_subject` is intentionally
  1:1). `ListBuckets` returns exactly the caller's own bucket.
- **A key is a path.** Folder paths map to key prefixes; a key resolves to a
  `(owner_subject, path)` pair. A read serves the current version; encrypted
  content is decrypted on the way out and listings report plaintext sizes.
- **A foreign bucket is `NoSuchBucket`.** Addressing a bucket that is not the
  caller's own raises `NoSuchBucketError`, identically for every foreign
  subject, so a bucket that never existed and one you may not see are
  indistinguishable — existence cannot be probed.

### The reserved `shared/` prefix

Nodes shared *with* the caller are reachable under a reserved
`shared/<owner-subject>/…` prefix inside the caller's own bucket
(`SHARED_PREFIX = "shared"`, `resolve_key` / `key_for`). Under this prefix only
nodes beneath a root the caller holds a grant on are reachable, and `shared/`
appears as a `CommonPrefix` at the bucket root in a listing.

The name `shared` is reserved at the root of every tree (enforced at
folder-create in `NodeService`) so a real folder can never shadow the shared
view. Addressing your own tree through the prefix is refused with
`InvalidArgument`, so one node is never reachable under two keys.

A write under `shared/` by a caller who holds only `viewer` on the subtree is
refused with `AccessDenied`, because the delegated `ContentService.upload`
requires `EDITOR` on the parent — there is no parallel permission check in the
S3 layer.

## Supported operations

Dispatched by method and query parameters in
`adapters/inbound/api/routers/s3.py`, all delegating to `S3ObjectService`
(`application/s3_objects.py`):

| Operation | Shape |
|-----------|-------|
| `ListBuckets` | `GET /` |
| `HeadBucket` | `HEAD /{bucket}` |
| `ListObjectsV2` | `GET /{bucket}` — `prefix`, `delimiter`, `CommonPrefixes`, `max-keys`, `continuation-token`, `start-after`; over the caller's authorized view only, excluding trashed nodes |
| `HeadObject` | `HEAD /{bucket}/{key}` |
| `GetObject` | `GET /{bucket}/{key}` — supports `Range`, decrypts encrypted content, emits a download audit record |
| `PutObject` | `PUT /{bucket}/{key}` — through the existing upload use case, so quota, versioning, and encryption inheritance apply |
| `CopyObject` | `PUT /{bucket}/{key}` with `x-amz-copy-source` — server-side, carrying no grants onto the copy |
| `DeleteObject` | `DELETE /{bucket}/{key}` — a soft delete, recoverable for the trash window |
| `DeleteObjects` | `POST /{bucket}?delete` — a batch of soft deletes, each key reported independently |
| Multipart | `CreateMultipartUpload` (`POST …?uploads`), `UploadPart` (`PUT …?partNumber&uploadId`), `CompleteMultipartUpload` (`POST …?uploadId`), `AbortMultipartUpload` (`DELETE …?uploadId`), `ListParts` (`GET …?uploadId`). Quota is charged on completion; an abandoned upload is reclaimed after `S3_MULTIPART_ABANDON_HOURS`. |
| Presigned URLs | Generated by `S3PresignService` (`application/s3_presign.py`); every URL addresses CyberFS's own endpoint and is signed with a CyberFS access-key secret, so following it transits CyberFS subject to authorization, quota, and decryption. Revoking the signing key stops the URL working on the very next request. |

Errors are always rendered in S3's `<Error>` XML shape (code, message, request
id), including authentication failures — never RFC 7807 problem+json.

## Deliberately unsupported

| Operation | Response | Why |
|-----------|----------|-----|
| `CreateBucket`, `DeleteBucket` | refused (`BucketManagementRefusedError`) | A bucket corresponds to a user, not a client-managed resource. Buckets are created and removed by user lifecycle, not by an S3 call. |
| Any bucket other than the caller's own | `NoSuchBucket` | There are no cross-user buckets. A subject sees only their own bucket plus the reserved `shared/` view; a foreign subject's bucket is indistinguishable from one that never existed. |
| S3 object-versioning parameters (`versionId` on read/copy) | ignored | CyberFS versioning is not S3 object versioning. A read or copy always serves the current version, so a version id in the query or `x-amz-copy-source` is stripped rather than honoured. |
| Object/bucket sub-resource APIs — `acl`, `tagging`, `versioning`, `versions`, `policy`, `cors`, `lifecycle`, `website`, `logging`, `location`, `replication`, `encryption`, `object-lock`, `legal-hold`, `retention`, `torrent`, `restore`, `select`, `accelerate`, `analytics`, `inventory`, `metrics`, `notification`, `ownershipcontrols`, `publicaccessblock`, `requestpayment` | `NotImplemented` | These configure behaviour CyberFS models elsewhere (encryption, retention, sharing) or not at all. Rejecting them explicitly stops a sub-resource request being mistaken for a plain list or object read (`_UNSUPPORTED_SUBRESOURCES` / `_ensure_supported`). |

See also [encryption.md](encryption.md) for how object bytes are encrypted at
rest, [sharing.md](sharing.md) for the grant model behind the `shared/` prefix,
and [auth-integration.md](auth-integration.md) for how bearer tokens are
verified.
