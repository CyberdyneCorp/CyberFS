# Content encryption

CyberFS can seal file content at rest so that neither the object store nor an
administrator can read it. Encryption is **optional per file** and turned on by
inheritance or by explicit request — an unencrypted deployment stores plaintext
and pays none of this cost.

This document describes what the code in
[`src/cyberfs/domain/keys.py`](../src/cyberfs/domain/keys.py),
[`src/cyberfs/domain/framing.py`](../src/cyberfs/domain/framing.py),
[`src/cyberfs/application/encryption.py`](../src/cyberfs/application/encryption.py),
[`src/cyberfs/adapters/outbound/crypto.py`](../src/cyberfs/adapters/outbound/crypto.py),
and [`src/cyberfs/adapters/outbound/cipher.py`](../src/cyberfs/adapters/outbound/cipher.py)
actually does. The behaviour is specified in
[`content-encryption/spec.md`](../openspec/changes/bootstrap-cyberfs/specs/content-encryption/spec.md).

## Key hierarchy

Encrypted content is protected by a three-level AES-256-GCM envelope. Each level
wraps the one below; only wrapped forms are ever persisted.

```
MASTER_KEY  ->  per-user KEK  ->  per-file DEK  ->  content frames
 (env var)      (one per user)    (one per file     (AES-256-GCM,
                                   version)           framed)
```

- **`MASTER_KEY`** — a deployment-wide 256-bit key supplied through the
  environment. It is provided as base64 that must decode to exactly 32 bytes
  (`decode_master_key`, `MASTER_KEY_BYTES = 32` in
  [`settings.py`](../src/cyberfs/infrastructure/settings.py)). In production the
  service refuses to start if it is the development placeholder. `MASTER_KEY`
  wraps each user's KEK and is the only key CyberFS never persists — it lives
  solely in the process environment.

- **Per-user KEK** (key-encryption key) — a fresh 256-bit key generated from the
  OS CSPRNG when a user is provisioned (`generate_kek`). It is stored only in
  wrapped form (`UserKey.wrapped_kek`) sealed under `MASTER_KEY` with associated
  data `cyberfs/kek/v1`. Each wrapped KEK records the `master_key_id` that
  sealed it — a short, non-reversible SHA-256 digest label — so a rotation can
  tell what it has already rewrapped.

- **Per-file DEK** (data key) — a fresh 256-bit key generated per encrypted file
  version (`generate_key`), unique to that version and never reused across files
  or versions. It is stored wrapped under the owner's KEK with associated data
  `cyberfs/dek/v1`, as a `WrappedDataKey` row per `(node, subject)` that may read
  the file. A DEK never leaves the process unwrapped.

The distinct associated-data labels mean a wrapped KEK can never be replayed
where a wrapped DEK is expected, or vice versa. Unwrapped KEKs and DEKs exist in
process memory only for the duration of a request — never in Postgres, MinIO,
Redis, a log line, or an error response.

### Key access follows sharing

The set of principals who can obtain a file's DEK is exactly the set authorized
to read it. Sharing an encrypted file unwraps the DEK with the sharer's KEK and
stores a copy wrapped under the recipient's KEK, in the same transaction as the
grant — **content objects are never touched** (`EncryptionService.rewrap_for`).
Revoking a grant deletes the recipient's wrapped copies in the same transaction,
so a revoked recipient has no key to unwrap even if they replay a captured
request. A recipient who has never used CyberFS has no KEK yet, and the share is
refused rather than storing a key nobody can open.

## Framed content format

Encrypted content is not sealed as one blob — a single AEAD seal over a whole
file cannot be decrypted without buffering it and cannot serve a range at all.
Instead content is sealed in fixed-size frames
([`domain/framing.py`](../src/cyberfs/domain/framing.py),
[`adapters/outbound/cipher.py`](../src/cyberfs/adapters/outbound/cipher.py)).

A stored object is:

```
header  ||  frame[0]  ||  frame[1]  ||  ...  ||  frame[N] (final)
```

- **Header** (11 bytes): `MAGIC("CFSENC", 6) || format_version(1) || frame_size(4, big-endian)`.
  The magic makes a non-CyberFS object detectable rather than misparsed; the
  version makes a future format change explicit.
- **Each frame**: `nonce(12) || AES-256-GCM(plaintext || tag(16))`. Per-frame
  overhead is 28 bytes (`FRAME_OVERHEAD`). Every frame draws a fresh 96-bit
  nonce; since a DEK is unique to one file version, random nonces sit far below
  the birthday bound.
- **Frame size**: default 64 KiB (`ENCRYPTION_FRAME_BYTES=65536`), constrained to
  1 KiB–8 MiB. It is stored in the header so a range read that starts mid-object
  can recover it.

### Associated data binds structure

Each frame's tag commits to more than its own bytes (`associated_data`):

```
version_id(16)  ||  frame_index(8, big-endian)  ||  final_flag(1)
```

This makes three attacks detectable that plain AEAD would accept:

- **Reordering** — swapping two frames fails because the index is authenticated.
- **Truncation** — the final frame is marked; a stream that ends without one is
  rejected as truncated. AEAD alone happily accepts a valid prefix.
- **Cross-version substitution** — a frame from another version of the same file
  cannot be substituted, even though both were sealed under the same file's key
  lineage, because the version id is bound.

Any tampering raises `IntegrityFailureError`; the download aborts and the error
is deliberately opaque — no nonce, tag, or ciphertext fragment reaches the
caller.

Because content is framed, decryption **streams** frame by frame without
materializing the whole plaintext, and a `Range` request fetches and decrypts
only the frames covering the requested bytes (`plan_range` / `frames_for_range`).

## Optional encryption

Encryption is opt-in. Every file carries an `encrypted` flag fixed at creation;
every folder carries an `encryption_default` of `inherit`, `on`, or `off`.

When a file is created, `resolve_encryption` decides whether to encrypt with this
precedence:

1. An explicit per-request choice wins (the `encrypted` query parameter on the
   upload endpoint, or `EncryptionRequest.encrypted`).
2. Otherwise the nearest ancestor folder that states `on` or `off` decides.
3. Otherwise the deployment default `ENCRYPTION_DEFAULT_ON` (default `false`).

Changing a folder's default never re-encrypts files already inside it. Changing
an existing file's state is an explicit, owner-only operation
(`PUT /nodes/{node_id}/encryption`,
[`routers/content.py`](../src/cyberfs/adapters/inbound/api/routers/content.py))
that rewrites content into a new version; a caller holding only `editor` gets
`403`. Either direction writes a fresh version and records the change in the
audit log (`ENCRYPTION_ENABLED` / `ENCRYPTION_DISABLED`); retained older versions
keep their existing objects, so plaintext copies from before an encrypt operation
stay downloadable until they age out under the version-retention limit.
Decrypting additionally destroys the node's wrapped data keys once the plaintext
version is committed.

## Threat model

### What encryption protects

- **Confidentiality of content at rest.** The bytes written to MinIO are
  ciphertext; reading a stored object directly yields no plaintext.
- **Confidentiality against administrators.** No administrative privilege yields
  plaintext. `is_admin` grants access to metadata and aggregate statistics only.
  An admin download of a file they do not own and were not granted returns
  `403`, encrypted or not; an admin cannot self-grant; no admin endpoint ever
  returns a DEK, a KEK, or `MASTER_KEY`, wrapped or unwrapped. There is **no key
  escrow**.
- **Integrity of stored ciphertext.** Tampering, reordering, truncation, and
  cross-version substitution are all detected (see above).
- **Effective revocation.** A revoked recipient's wrapped keys are gone, so a
  replayed request has nothing to unwrap.

### What encryption does not protect

- **Metadata.** File and folder names, sizes, MIME types, timestamps, the tree
  structure, and sharing relationships live in Postgres in plaintext. Encryption
  covers content bytes only.
- **File sizes.** The stored ciphertext size is a fixed function of the plaintext
  size (header + per-frame overhead, `ciphertext_size`), so the plaintext length
  is derivable from the object size. Sizes are not hidden.
- **A compromised running host.** `MASTER_KEY` is in the process environment and
  unwrapped keys pass through process memory during a request. An attacker who
  compromises a running instance with `MASTER_KEY` present can unwrap KEKs and
  DEKs and read content. Encryption defends the object store and the database at
  rest, and defends against administrative access — not against code execution on
  a live host holding the master key.
- **Unencrypted files.** Files stored with `encrypted: false` are plaintext in
  MinIO and enjoy none of the above.

## Master key readiness

At startup and on every readiness check, the `encryption` health probe
([`composition.py`](../src/cyberfs/adapters/inbound/api/composition.py),
`EncryptionService.verify_master_key`) confirms the configured `MASTER_KEY` can
still unwrap stored key material. If it cannot, readiness reports the component
`DOWN` with "master key cannot open stored key material", taking the replica out
of rotation rather than serving a `500` per encrypted file. A deployment with no
wrapped keys yet passes trivially.

## Rotation

Both rotations rewrap key material only — **content objects are never rewritten**
— and both are resumable, so an interruption leaves everything still openable and
rerunning finishes the remainder.

### Rotating `MASTER_KEY`

`MASTER_KEY_PREVIOUS` exists solely for this. To rotate:

1. Set the new key in `MASTER_KEY` and the outgoing key in `MASTER_KEY_PREVIOUS`.
   With both configured, `MasterKeyProvider` can unwrap a KEK sealed under
   *either* key (matched by its recorded `master_key_id`) while wrapping any new
   material only under the current key.
2. Run the rotation (`EncryptionService.rotate_master_key`). It rewraps every KEK
   still sealed under an older master under the current one, in batches, and
   records a `KEY_ROTATED` audit entry. Because each pass looks only for stale
   keys, rerunning after an interruption completes what was left.
3. Once every KEK is rewrapped, clear `MASTER_KEY_PREVIOUS`.

The service stays readable throughout, since the previous key keeps opening
material that has not moved yet.

### Rotating a user's KEK

`EncryptionService.rotate_user_key` mints a new KEK, rewraps every DEK sealed
under the user's old KEK, and only then replaces the stored wrapped KEK — the old
KEK stays valid until every DEK has moved, so an interruption leaves everything
openable. The rewrap is audited.

### Rewrap is currently synchronous

When a folder containing encrypted files is shared, CyberFS rewraps the DEK of
every encrypted descendant for the recipient **synchronously, inside the grant
transaction** (`SharingService._rewrap_subtree`). The grant does not return until
every descendant key is rewrapped, which keeps the invariant "whoever can read
the file can obtain its key" true the instant the share exists.

`ASYNC_REWRAP_THRESHOLD_NODES` (default `100`,
[`settings.py`](../src/cyberfs/infrastructure/settings.py)) is defined for a
future path that would offload large-subtree rewraps to a background job above
the threshold. That path is **not yet wired**: the setting is currently unused
and all rewrapping happens synchronously regardless of subtree size.

## Configuration reference

From [`.env.example`](../.env.example) and
[`settings.py`](../src/cyberfs/infrastructure/settings.py):

| Variable | Default | Meaning |
|---|---|---|
| `MASTER_KEY` | dev placeholder | Base64 of exactly 32 bytes. Wraps every user KEK. Required; the production placeholder is rejected at startup. |
| `MASTER_KEY_PREVIOUS` | unset | The outgoing master key, set **only** while a rotation is in progress. |
| `ENCRYPTION_DEFAULT_ON` | `false` | Deployment-wide default when no folder or request expresses a choice. |
| `ENCRYPTION_FRAME_BYTES` | `65536` | Frame size for sealed content (1 KiB–8 MiB). |
| `ASYNC_REWRAP_THRESHOLD_NODES` | `100` | Reserved for a future async rewrap path; currently unused. |
