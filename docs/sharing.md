# Sharing in CyberFS

CyberFS shares access at the level of a **node** — a file or a folder — using
three ordered roles, subtree inheritance, and (for anonymous access) public
links. Identity is always a CyberdyneAuth subject; CyberFS keeps no user
password and issues no login of its own.

The whole model is small on purpose. Effective permission is a `max()` over a
handful of grants (`domain/permissions.py`), there is no deny rule, and every
change that touches encrypted content rewraps key material in the same
transaction (`application/sharing.py`). This document describes what the code
actually does; the requirements it implements live in
`openspec/changes/bootstrap-cyberfs/specs/sharing/spec.md`.

## Roles

Three roles, totally ordered by privilege (`domain/sharing.py`, `Role`):

```
viewer (10) < editor (20) < owner (30)
```

The integer values exist so that `max()` over a set of roles *is* the whole
resolution rule. Each role's capabilities (from `Role`'s capability properties):

| Role | can read | can write | can delete | can share | can change encryption |
|---|:---:|:---:|:---:|:---:|:---:|
| `viewer` | ✅ | — | — | — | — |
| `editor` | ✅ | ✅ | — | — | — |
| `owner`  | ✅ | ✅ | ✅ | ✅ | ✅ |

Concretely:

- **`viewer`** — read metadata, list a folder's children, download content.
- **`editor`** — everything a viewer can, plus create, rename, move within the
  shared subtree, replace content, and restore versions. An editor **cannot**
  re-share: `can_share` is `owner`-only, so an editor can change a file's
  contents but never who else can see it.
- **`owner`** — everything an editor can, plus delete, grant, revoke, transfer
  ownership, and change per-file encryption.

The node's owner always holds `owner` on that node and cannot be removed from
it: revoking the owner's own grant fails with `cannot_revoke_owner`
(`CannotRevokeOwnerError` → `409 Conflict`).

## Effective permission and inheritance

A grant on a folder applies to **every descendant** of that folder. A caller's
effective role on a node is the **highest** role among:

1. ownership of the node,
2. any direct grant on the node, and
3. any grant on **any ancestor** of the node.

This is computed by `resolve_effective_role(is_owner, granted)` in
`domain/permissions.py`, which is literally `max(candidates, default=None)`.
Because it is a maximum with no deny rule, the model is monotonic: adding a
grant can only *raise* a caller's access, never lower it. That is what keeps
"why can Bob read this?" answerable — walk from the node to the root and take
the highest grant found — and lets the permission cache be invalidated safely
by subject.

`require_role(effective, minimum, node_id=…)` enforces a threshold and encodes
an important distinction:

- a caller with **no** access at all gets `404 Not Found`, so the API does not
  disclose that a node it cannot see even exists;
- a caller who can see the node but lacks the required role gets `403
  Forbidden`, which discloses nothing they did not already know.

Some consequences that fall directly out of the maximum:

- **Highest role wins.** A user with `viewer` on an ancestor folder and
  `editor` directly on a file inside it has effective `editor` on that file.
- **A lower direct grant never narrows an inherited one.** `editor` on an
  ancestor plus `viewer` on a descendant still yields `editor`.
- **Moving a node changes inherited access immediately.** Access is resolved by
  walking ancestors on each request; there is no stored per-descendant grant to
  update. A file moved out of a shared folder loses inherited access at once, a
  file moved in gains it, and a newly created descendant inherits without any
  additional grant.

Effective permission is resolved against the ancestor chain up to
`ANCESTOR_GUARD_DEPTH = 512` levels (`application/sharing.py`).

## Granting and revoking

Grants are managed through the sharing router
(`adapters/inbound/api/routers/shares.py`, prefix `/api/v1`). All grant-changing
endpoints require a **`FreshPrincipal`** — an introspection-backed identity, not
just a cheap claim check — because widening or withdrawing access on the
strength of a token whose holder was demoted a minute ago is exactly the failure
this guards against.

| Method & path | Purpose | Notes |
|---|---|---|
| `PUT /api/v1/nodes/{node_id}/grants` | Grant a role | `201`, returns the full grant list |
| `GET /api/v1/nodes/{node_id}/grants` | List grants on a node | owner only |
| `DELETE /api/v1/nodes/{node_id}/grants/{subject}` | Revoke a grant | `204` |
| `GET /api/v1/shared-with-me` | Nodes shared with the caller | subtree roots only |
| `POST /api/v1/nodes/{node_id}/owner` | Transfer ownership | see below |

The grant body (`GrantRequest`) is:

```json
{ "recipient": "user@example.com", "role": "viewer" }
```

`recipient` is a CyberdyneAuth subject **or** an email that resolves to a user
within the sharer's orgs; `role` is one of `viewer`, `editor`, `owner`.

Behaviour to know about:

- **Owner-only.** Granting requires `owner` on the node (`_require_owned`). An
  inherited `editor` attempting to grant gets `403`.
- **Recipient must exist.** An email CyberdyneAuth does not resolve yields
  `recipient_unknown` → `404`. No pending or placeholder grant is created.
- **Regrant replaces.** Granting a role to someone who already holds a different
  role on the same node updates the existing grant (`Grant.with_role`) rather
  than creating a second one.
- **No self-grant.** Granting to yourself returns `cannot_share_with_self` →
  `409`.
- **Revocation is immediate.** On revoke, the recipient's cached permission
  decisions are invalidated *before* the response returns
  (`invalidate_permissions_for_subject`), not on a TTL — the recipient's very
  next request against that node and its descendants is denied.
- **A recipient may drop their own access.** `DELETE …/grants/{subject}`
  succeeds if the caller is either the node owner or the subject being removed;
  a non-owner may remove only their own grant, and doing so does not alter the
  node itself.
- **Independent grants survive.** Revoking a folder grant from a user who also
  holds a direct grant on a descendant leaves the descendant grant intact —
  again a direct consequence of the model being a maximum of independent grants.

`GET /api/v1/shared-with-me` returns the **topmost** node of each shared
subtree, not every inherited descendant: if you were granted a folder, you see
the folder, not each file inside it (`shared_with_me`).

## How sharing interacts with encryption

Encrypted files use envelope encryption: content is sealed with a per-file data
key (**DEK**), which is itself wrapped under each authorized user's
key-encryption key (**KEK**). Sharing an encrypted file therefore does not touch
the content objects at all — it **rewraps the DEK** for the recipient
(`application/encryption.py`, `rewrap_for`).

Every sharing operation that grants access to encrypted content rewraps key
material **inside the same transaction** as the grant. A grant that was visible
but whose DEK rewrap had failed would be worse than no grant at all — the
recipient would see a file they could never decrypt — so:

- On **grant**, `_rewrap_subtree` walks the node and its descendants and, for
  each `encrypted` node, calls `KeyRewrapper.rewrap_for` to store the recipient
  their own wrapped copy of the DEK. Plaintext-stored nodes are skipped.
- If the file is encrypted but the key service is unavailable, the grant **fails
  closed** with `403` (`_rewrap_one`) rather than creating a broken share.
- If the recipient has **never used CyberFS** and so has no KEK yet, rewrap
  raises `key_unavailable` — CyberFS refuses to store a key nobody can open.
- On **revoke**, `_revoke_keys` deletes the recipient's wrapped DEKs across the
  node and its subtree, so a replayed request finds nothing to unwrap.

Rewrapping moves only key material; the sealed content bytes in object storage
are never read or rewritten when access changes. For the encryption model
itself, see `application/encryption.py` and
`openspec/changes/bootstrap-cyberfs/specs/content-encryption/spec.md`.

## Public links

A public link grants **unauthenticated, read-only** access to one node and its
subtree — a link *is* the credential. A link's role is always `viewer` and there
is no way to widen it (`PublicLink.role`).

### Creation

`POST /api/v1/nodes/{node_id}/links` (owner only, `FreshPrincipal`). Body
(`CreateLinkRequest`):

```json
{ "expires_at": "2026-12-31T00:00:00Z", "passphrase": "optional-secret" }
```

Both fields are optional. `expires_at`, if given, must be in the future
(otherwise `422`). `passphrase` must be 4–255 characters.

The response (`IssuedLinkResponse`) contains the `token` **exactly once** — this
is the only time the token exists in cleartext outside the client. The token is
generated with `secrets.token_urlsafe(32)` (256 bits, `domain/links.py`),
encodes nothing about the node it points at, and is stored only as a SHA-256
hash. A database leak therefore yields no working links, and later listings
(`LinkSummary`) never echo the token back.

### Passphrase protection

When a link carries a passphrase, the plaintext is never stored: it is hashed
with **scrypt** (N=2¹⁴, r=8, p=1) using a fresh per-link salt, stored as
`scrypt$<salt>$<key>`, and verified in constant time (`hash_passphrase` /
`verify_passphrase`). The client supplies it on access via the
**`X-Link-Passphrase`** request header.

### Access

Two unauthenticated endpoints resolve a link by token:

| Method & path | Purpose |
|---|---|
| `GET /api/v1/public/{token}` | Open the link — returns the node's metadata |
| `GET /api/v1/public/{token}/content` | Download the linked file (supports `Range`) |

Both accept the optional `X-Link-Passphrase` header. Content is streamed **as
the node's owner**, so the link carries exactly the owner's read access and
never more. A link on a folder exposes only that folder's subtree — there is no
traversal to its parent or siblings.

### Expiry, revocation, and rate limits

- **Expiry / revocation are indistinguishable from "never existed".** A token
  that is revoked, expired, or was never issued all return the same `404 Not
  Found` (`resolve_link` checks `is_usable`), so a caller cannot learn that a
  link once existed.
- **Revocation** — `DELETE /api/v1/links/{link_id}` (owner only,
  `FreshPrincipal`) sets `revoked_at`; subsequent requests with that token
  `404` immediately.
- **Rate limiting.** Passphrase attempts are limited per link and source IP by a
  fixed-window limiter (`FixedWindowLimiter`, 1-minute window). The limit is
  `PUBLIC_LINK_MAX_ATTEMPTS_PER_MIN` (default **10**; see `settings.py`
  `public_link_max_attempts_per_min` and `.env.example`). Exceeding it returns
  `429 Too Many Requests` with a `Retry-After` hint (`RateLimitedError`); an
  incorrect passphrase within the limit returns `403 Forbidden`.

> Note: the spec describes an incorrect passphrase as `401`; the implementation
> raises `PermissionDeniedError`, which the error map
> (`adapters/inbound/api/errors.py`) renders as **`403 Forbidden`**. A
> rate-limited attempt is **`429`**.

### Listing

`GET /api/v1/nodes/{node_id}/links` (owner only) returns each link as the owner
sees it (`LinkSummary`): id, node, creator, timestamps, `expires_at`, whether it
is `revoked`, whether it is `passphrase_protected`, its `access_count`, and
`last_accessed_at`. The token is never included.

## Ownership transfer

`POST /api/v1/nodes/{node_id}/owner` transfers a node and its whole subtree to
another user (`TransferRequest`):

```json
{ "recipient": "user@example.com", "keep_editor_access": true }
```

Owner-only, `FreshPrincipal`. What happens (`transfer_ownership`):

- **Quota moves with the data.** The subtree's file bytes are charged to the new
  owner and released from the previous owner in the same transaction. If the
  recipient's quota cannot accommodate the subtree, the transfer is rejected
  with `quota_exceeded` → `507 Insufficient Storage` and nothing changes.
- **Recipient must be a known user.** A recipient with no CyberFS record yet
  yields `recipient_unknown` → `404`.
- **Encrypted files are rewrapped first.** Each encrypted file's DEK is rewrapped
  for the new owner *before* ownership flips, so any rewrap failure aborts the
  entire transfer rather than leaving unreadable files behind.
- **The previous owner keeps `editor`** by default (`keep_editor_access: true`),
  left as an explicit grant; set it to `false` to transfer cleanly away.

## Auditing

Every sharing change is recorded to the audit log (`AuditAction`,
`application/sharing.py`): grant created/updated, grant revoked, public link
created / accessed / revoked, and ownership transferred. Public-link access
records the link identifier and source IP but **never** the link secret. Audit
records are immutable through the API.

## Configuration

| Env var | Setting | Default | Effect |
|---|---|---|---|
| `PUBLIC_LINK_MAX_ATTEMPTS_PER_MIN` | `public_link_max_attempts_per_min` | `10` | Passphrase attempts per link + IP per minute before `429` |

## See also

- `application/encryption.py` — the envelope-encryption model behind DEK rewrap.
- `docs/auth-integration.md` — how CyberdyneAuth identities (subjects) are resolved.
- `openspec/changes/bootstrap-cyberfs/specs/sharing/spec.md` — the sharing requirements.
