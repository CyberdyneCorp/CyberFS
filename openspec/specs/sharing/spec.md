# sharing Specification

## Purpose
TBD - created by archiving change bootstrap-cyberfs. Update Purpose after archive.
## Requirements
### Requirement: Permission roles

CyberFS SHALL define exactly three roles, totally ordered by privilege: `viewer` < `editor` < `owner`.

- `viewer` SHALL permit reading metadata, listing folder children, and downloading content.
- `editor` SHALL permit everything `viewer` permits plus creating, renaming, moving within the shared subtree, replacing content, and restoring versions.
- `owner` SHALL permit everything `editor` permits plus deleting, granting, revoking, and transferring ownership.

The node's owner SHALL always hold the `owner` role on that node and SHALL NOT be removable from it.

#### Scenario: Viewer cannot write

- **WHEN** a caller holding `viewer` attempts to replace a file's content
- **THEN** the system SHALL respond `403 Forbidden` and SHALL make no change

#### Scenario: Editor cannot delete

- **WHEN** a caller holding `editor` attempts to delete the shared node
- **THEN** the system SHALL respond `403 Forbidden`

#### Scenario: Editor cannot re-share

- **WHEN** a caller holding `editor` attempts to grant access to a third user
- **THEN** the system SHALL respond `403 Forbidden`

#### Scenario: Owner grant cannot be revoked from the owner

- **WHEN** any caller attempts to remove the node owner's own access
- **THEN** the system SHALL respond `409 Conflict` with error code `cannot_revoke_owner`

### Requirement: Granting access to a user

A caller holding `owner` on a node SHALL be able to grant any of the three roles on that node to another CyberdyneAuth user identified by subject or email.

#### Scenario: Grant created

- **WHEN** an owner grants `viewer` on a file to another user
- **THEN** the system SHALL persist the grant, respond `201 Created`, and the recipient SHALL immediately be able to read the file

#### Scenario: Grant to unknown user

- **WHEN** an owner grants access to an email that CyberdyneAuth does not resolve to a user
- **THEN** the system SHALL respond `404 Not Found` with error code `recipient_unknown` and SHALL NOT create a pending grant

#### Scenario: Regrant updates the role

- **WHEN** an owner grants a role to a user who already holds a different role on the same node
- **THEN** the system SHALL replace the existing grant rather than creating a second one

#### Scenario: Self-grant rejected

- **WHEN** an owner grants access to themselves
- **THEN** the system SHALL respond `409 Conflict` with error code `cannot_share_with_self`

#### Scenario: Grant on a node the caller does not own

- **WHEN** a caller who holds only an inherited `editor` role attempts to grant access
- **THEN** the system SHALL respond `403 Forbidden`

### Requirement: Inheritance and effective permission

A grant on a folder SHALL apply to every descendant of that folder. A caller's effective permission on a node SHALL be the highest role among: ownership of the node, any direct grant on the node, and any grant on any ancestor of the node.

#### Scenario: Folder grant reaches descendants

- **WHEN** a user is granted `viewer` on a folder
- **THEN** they SHALL be able to read every existing and future descendant file of that folder

#### Scenario: Highest role wins

- **WHEN** a user holds `viewer` on an ancestor folder and `editor` directly on a file inside it
- **THEN** their effective permission on that file SHALL be `editor`

#### Scenario: Ancestor grant is not narrowed by a lower direct grant

- **WHEN** a user holds `editor` on an ancestor folder and `viewer` directly on a descendant file
- **THEN** their effective permission on that file SHALL be `editor`

#### Scenario: Moving out of a shared folder ends inherited access

- **WHEN** a file is moved out of a folder from which a user inherited access, and the user holds no direct grant
- **THEN** that user SHALL immediately lose access to the file

#### Scenario: Moving into a shared folder grants inherited access

- **WHEN** a file is moved into a folder shared with a user
- **THEN** that user SHALL immediately gain the folder's role on the file

#### Scenario: New descendant inherits immediately

- **WHEN** a file is created inside a folder already shared with a user
- **THEN** that user SHALL be able to access the new file without any additional grant

### Requirement: Async rewrap for large subtrees

Sharing an encrypted subtree rewraps every descendant's data key for the recipient. When the subtree holds more encrypted nodes than `ASYNC_REWRAP_THRESHOLD_NODES`, the rewrap SHALL be handed to a background worker and the grant SHALL be created pending, becoming usable only once the worker has rewrapped every encrypted descendant for the recipient. A pending grant SHALL confer no access and SHALL NOT appear in the recipient's view, so a partially rewrapped share never appears usable. At or below the threshold the rewrap SHALL remain synchronous and the grant immediately usable.

#### Scenario: Small subtree stays synchronous

- **WHEN** an owner shares a subtree at or below `ASYNC_REWRAP_THRESHOLD_NODES` encrypted nodes
- **THEN** the system SHALL rewrap every descendant key inside the grant transaction and the recipient SHALL be able to read immediately, with no pending state

#### Scenario: Large subtree is created pending and confers no access

- **WHEN** an owner shares a subtree with more than `ASYNC_REWRAP_THRESHOLD_NODES` encrypted nodes
- **THEN** the system SHALL create the grant pending, SHALL NOT rewrap inline, and until the worker completes the recipient SHALL be denied every node under it and it SHALL NOT appear in their shared-with-me listing or search

#### Scenario: Worker activation grants access atomically

- **WHEN** the background worker finishes rewrapping every encrypted descendant of a pending grant for the recipient
- **THEN** the grant SHALL become active, the recipient SHALL thereafter be able to read every encrypted descendant present at activation, and their cached permission decisions SHALL be dropped

#### Scenario: Interrupted rewrap leaves the grant pending

- **WHEN** the worker is interrupted partway through a pending grant's subtree
- **THEN** the grant SHALL remain pending — conferring no access — and a subsequent run SHALL complete it without wrapping any key twice

#### Scenario: File created while pending is rewrapped before activation

- **WHEN** an encrypted file is created under the shared folder while its grant is still pending
- **THEN** the worker SHALL rewrap that file for the recipient before activating the grant, so no file present at activation is left undecryptable

#### Scenario: Revoking a pending grant

- **WHEN** an owner revokes a grant that is still pending
- **THEN** the system SHALL delete the grant and any partially rewrapped keys, and the recipient SHALL be denied

#### Scenario: Owner sees pending status

- **WHEN** an owner lists the grants on a node with a pending share
- **THEN** the listing SHALL include that grant with its pending status

### Requirement: Listing and revoking grants

CyberFS SHALL let a node owner list all grants on a node, let any user list nodes shared with them, and let an owner revoke any grant.

#### Scenario: Owner lists grants

- **WHEN** an owner requests the grants on a node
- **THEN** the system SHALL return each recipient, their role, who granted it, and when

#### Scenario: Shared-with-me listing

- **WHEN** a user lists items shared with them
- **THEN** the system SHALL return the topmost node of each shared subtree rather than every inherited descendant

#### Scenario: Revocation is immediate

- **WHEN** an owner revokes a grant
- **THEN** the recipient's next request against that node and its descendants SHALL be denied, with no reliance on cache expiry

#### Scenario: Revocation does not affect independent grants

- **WHEN** a folder grant is revoked from a user who also holds a direct grant on a descendant file
- **THEN** that user SHALL retain access to that file only

#### Scenario: Recipient may remove their own access

- **WHEN** a recipient removes a share from their own "shared with me" list
- **THEN** the system SHALL revoke that grant and SHALL NOT alter the node itself

### Requirement: Public links

An owner SHALL be able to create a public link to a file or folder that grants `viewer` access without CyberdyneAuth authentication. Public links SHALL carry an unguessable token, SHALL support an optional expiry and an optional passphrase, and SHALL be revocable.

#### Scenario: Public link grants read access

- **WHEN** an unauthenticated client presents a valid, unexpired public-link token
- **THEN** the system SHALL serve the linked node's metadata and content as `viewer`

#### Scenario: Public link never grants write

- **WHEN** a public-link holder attempts any mutation
- **THEN** the system SHALL respond `403 Forbidden`

#### Scenario: Expired link rejected

- **WHEN** a public link's expiry has passed
- **THEN** the system SHALL respond `404 Not Found` and SHALL NOT reveal that the link previously existed

#### Scenario: Passphrase enforced

- **WHEN** a public link carries a passphrase and the client supplies an incorrect one
- **THEN** the system SHALL respond `401 Unauthorized` and SHALL rate limit repeated attempts on that token

#### Scenario: Revoked link stops working immediately

- **WHEN** the owner revokes a public link
- **THEN** subsequent requests with that token SHALL respond `404 Not Found`

#### Scenario: Public link token is unguessable

- **WHEN** a public link is created
- **THEN** its token SHALL contain at least 128 bits of cryptographic randomness and SHALL NOT encode the node identifier

#### Scenario: Public link on a folder does not expose siblings

- **WHEN** a public link targets a folder
- **THEN** it SHALL expose only that folder's subtree and SHALL NOT permit traversal to its parent or siblings

### Requirement: Ownership transfer

An owner SHALL be able to transfer ownership of a node and its subtree to another user, who SHALL thereafter be charged for its storage.

#### Scenario: Transfer moves quota

- **WHEN** ownership of a subtree is transferred
- **THEN** the subtree's bytes SHALL be released from the previous owner's usage and charged to the new owner

#### Scenario: Transfer rejected when recipient lacks quota

- **WHEN** the recipient's quota cannot accommodate the subtree
- **THEN** the system SHALL respond `507 Insufficient Storage` and SHALL make no change

#### Scenario: Previous owner retains editor access

- **WHEN** ownership is transferred
- **THEN** the previous owner SHALL be left with an explicit `editor` grant unless the transfer requested otherwise

#### Scenario: Transfer requires accepting the encryption rewrap

- **WHEN** the transferred subtree contains encrypted files
- **THEN** the system SHALL rewrap each file's data key for the new owner as part of the same transaction, and SHALL abort the transfer if any rewrap fails

### Requirement: Share auditing

CyberFS SHALL record every grant, regrant, revocation, ownership transfer, public-link creation, and public-link use, with actor, target node, recipient, role, and timestamp.

#### Scenario: Grant audited

- **WHEN** a grant is created
- **THEN** an audit record SHALL be written identifying the granting user, the recipient, the node, and the role

#### Scenario: Public link access audited

- **WHEN** a public link is used to download content
- **THEN** an audit record SHALL be written with the link token identifier and the source IP, and SHALL NOT include the link secret

#### Scenario: Audit records are immutable

- **WHEN** any caller, including an administrator, attempts to modify or delete an audit record through the API
- **THEN** the system SHALL respond `403 Forbidden`

