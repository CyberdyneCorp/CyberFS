## MODIFIED Requirements

### Requirement: Soft delete, restore, and purge

Deletion SHALL move a node into a recoverable state for `TRASH_RETENTION_DAYS`, after which it SHALL be purged permanently. Purge SHALL delete both the metadata and the underlying objects. Purge SHALL also be available on demand for a node already in the trash, releasing its quota immediately.

#### Scenario: Deleted node hidden from listings

- **WHEN** a node is soft-deleted
- **THEN** the system SHALL exclude it from folder listings and search while retaining it in the owner's trash view

#### Scenario: Restore returns node to its parent

- **WHEN** the owner restores a soft-deleted node whose parent still exists and is not deleted
- **THEN** the system SHALL make it visible again in that parent

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
