## Why

Tooling that mounts a remote filesystem speaks WebDAV. `rclone`, `davfs2`, Windows
Explorer, macOS Finder and every Linux file manager can mount a WebDAV endpoint
with no client software written by us. CyberFS speaks REST and S3, and neither is
what a file manager reaches for.

This also delivers FUSE without a driver. `rclone mount` turns any WebDAV or S3
endpoint into a local filesystem via FUSE, so once the WebDAV surface exists,
"mount CyberFS as a drive" is a documented `rclone` invocation rather than a
program we maintain. That is the whole of the FUSE ask, and the reason no FUSE
code appears in this change.

## What Changes

- A WebDAV surface at `WEBDAV_BASE_PATH` (default `/webdav`), mounted **by
  default**. `WEBDAV_ENABLED` defaults true and exists to switch it off; a
  deployment that sets it false exposes no such routes at all.
- **Class 1 compliance**: `OPTIONS`, `PROPFIND` (Depth 0 and 1), `GET`, `HEAD`,
  `PUT`, `DELETE`, `MKCOL`, `COPY`, `MOVE`. Advertised as `DAV: 1` so a client
  knows what it is talking to.
- **Authentication reuses the existing S3 access keys** over HTTP Basic. They are
  already a long-lived, revocable, audited credential that is deliberately "a
  credential, not an identity" — a WebDAV client that cannot perform an OAuth
  redirect is exactly what they were for. No new credential type, no new
  lifecycle, no new revocation path.
- Every operation goes through the existing use cases, so quotas, encryption,
  sharing, the trash, auditing and the activity feed behave identically whether a
  byte arrives over REST, S3 or WebDAV.
- **BREAKING for the recorded non-goals**: `bootstrap-cyberfs` and
  `add-s3-and-activity` both list WebDAV and FUSE as out of scope. This change
  reverses that for WebDAV, and answers FUSE by documentation rather than code.
  The reversal is recorded in this change's `design.md`, the way the S3 change
  recorded its own.

## Capabilities

### New Capabilities

- `webdav-compatibility`: the WebDAV protocol surface — the methods, the
  multistatus XML, property mapping, Basic authentication over access keys, and
  what is deliberately not implemented.

### Modified Capabilities

- `authentication`: "S3 access keys are a credential, not an identity" gains
  WebDAV as a second consumer, so the requirement stops implying the keys are
  S3-only.

## Impact

**Affected code:**

- `src/cyberfs/adapters/inbound/api/routers/webdav.py` — the surface, built like
  `routers/s3.py`: one router factory, every failure rendered as WebDAV XML.
- `src/cyberfs/domain/webdav/` — multistatus XML generation and property mapping,
  pure and testable without HTTP.
- `src/cyberfs/application/webdav_auth.py` — Basic credential verification against
  `S3AccessKeyService`, with the same constant-time discipline `s3_auth.py` uses.
- `src/cyberfs/infrastructure/settings.py` — `WEBDAV_ENABLED`, `WEBDAV_BASE_PATH`.
- `docs/webdav.md` — how to mount with `rclone`, `davfs2`, Finder and Explorer,
  and the `rclone mount` invocation that provides FUSE.

**Not implemented, deliberately** — each stated in the spec so a client author
knows before trying:

- **`LOCK`/`UNLOCK` (Class 2).** There is no lock concept anywhere in CyberFS, and
  inventing one to satisfy a protocol would put a second, weaker concurrency
  model beside the `If-Match` revision checks that already exist. Windows
  Explorer and Finder both want Class 2 and degrade to read-only or refuse
  writes without it; that limitation is the cost, and `rclone` and `davfs2` do
  not care.
- **`PROPPATCH`.** Tags and metadata exist and are the right home for
  caller-defined properties, but mapping arbitrary dead properties onto them
  raises questions about the reserved namespace and the limits that deserve their
  own change.

**Security posture, and it is the main cost of this change.** Basic
authentication sends the secret on every request, and the surface is mounted by
default — so unlike `S3_API_ENABLED`, which defaults false, this reaches every
deployment whether or not anyone asked for it. Two consequences the
implementation must carry rather than assume:

- **Refusing plaintext is load-bearing, not a nicety.** A deployment that never
  opted in must not leak a credential per request because it terminates TLS
  somewhere unexpected. Production refuses a plaintext request outright.
- **Nothing is revealed before authentication.** `OPTIONS` advertises the methods
  and nothing about content, and every other method authenticates before it
  discloses whether a path exists, so a mounted-by-default surface is not a
  namespace oracle.

The access key is already sealed under `MASTER_KEY` and revocable in one write, so
a leaked key is contained the way a leaked S3 key is. Basic simply makes leakage
easier, which is what the two rules above answer.

**No schema change.** No migration, no new table, no new credential.
