## MODIFIED Requirements

### Requirement: Soft delete, restore, and purge

Deletion SHALL move a node into a recoverable state for `TRASH_RETENTION_DAYS`, after which it SHALL be purged permanently. Purge SHALL delete both the metadata and the underlying objects. Purge SHALL also be available on demand for a node already in the trash, releasing its quota immediately. Restore SHALL be refused rather than partially applied when the name it would reoccupy is no longer free.

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

#### Scenario: Restore onto a name that has been taken is refused

- **WHEN** the owner restores a trashed node whose name a live sibling has taken since the deletion, which the rule permitting a deleted node's name to be reused allows
- **THEN** the system SHALL respond `409 Conflict` with error code `name_taken`, and SHALL restore nothing — neither the entry nor any descendant it would have lifted

#### Scenario: Restore to a deleted parent

- **WHEN** the owner restores a node whose original parent has been purged
- **THEN** the system SHALL restore it into the owner's root folder

#### Scenario: Restore beneath a still-trashed parent

- **WHEN** the owner restores a node by identifier while the folder that contained it is still in the trash
- **THEN** the system SHALL restore it into the owner's root folder rather than into an invisible parent

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

## ADDED Requirements

### Requirement: Trash listing

CyberFS SHALL expose the caller's soft-deleted nodes as a paginated listing, so a node can be found and restored without the caller having retained its identifier. The listing SHALL present one entry per deletion rather than one per affected node, and each entry SHALL carry enough information to choose between restoring and purging it without a further request. An entry is a restore-or-purge handle rather than a metadata read of the node, so it SHALL NOT carry the content digest or the caller's effective permission.

#### Scenario: A deleted file can be found again

- **WHEN** the owner soft-deletes a file and later lists their trash
- **THEN** the file SHALL appear as an entry carrying an identifier they can restore, so deletion and restoration form a loop the caller can complete without having kept the identifier

#### Scenario: A deleted folder is one entry, not one per descendant

- **WHEN** the owner soft-deletes a folder containing descendants and lists their trash
- **THEN** exactly one entry SHALL appear, for the folder, and no descendant removed by that deletion SHALL appear as a separate entry

#### Scenario: An entry reports what restoring it would bring back

- **WHEN** a trash entry for a folder is listed
- **THEN** it SHALL report the total content bytes and the number of nodes that restoring it would return, rather than reporting the folder's own size of zero

#### Scenario: An entry reports where it came from

- **WHEN** a trash entry is listed
- **THEN** it SHALL report the path the node occupied when it was deleted, derived from the ancestors it will be restored beneath

#### Scenario: An entry reports its deadline

- **WHEN** a trash entry is listed
- **THEN** it SHALL report when it was deleted and when the retention sweep will destroy it, derived from `TRASH_RETENTION_DAYS`

#### Scenario: A node trashed beneath another trashed node is folded into it

- **WHEN** a node is in the trash and the folder containing it is also in the trash
- **THEN** only the outermost trashed node SHALL be listed as an entry, and the inner one SHALL become an entry of its own only once its parent is no longer trashed

#### Scenario: The listing reports how many entries the trash holds

- **WHEN** the trash is listed, whatever page is requested
- **THEN** the response SHALL report the total number of entries the trash currently holds, so the count that emptying the trash requires can be obtained in one request rather than by paginating the whole trash

#### Scenario: The trash is the owner's alone

- **WHEN** a caller who is not the owner lists the trash, including a recipient who held a share of a node before it was deleted
- **THEN** no entry for that node SHALL appear, because a soft delete withdraws access and the listing is scoped to nodes the caller owns

#### Scenario: The listing names no other user

- **WHEN** the trash listing is requested
- **THEN** it SHALL return only the caller's own trash, and there SHALL be no path or query parameter by which another user's trash could be requested

#### Scenario: Live nodes never appear

- **WHEN** the trash is listed
- **THEN** no node that has not been soft-deleted SHALL appear, and no root folder SHALL appear

#### Scenario: The listing is ordered and bounded

- **WHEN** the trash holds more entries than the requested page size
- **THEN** the system SHALL return entries most recently deleted first in a deterministic order, at most `PAGE_SIZE_MAX` of them, with a cursor for the next page, and every returned page SHALL be full while further entries remain

#### Scenario: The trash listing is never stale

- **WHEN** a node is deleted, restored, or purged and the trash is listed immediately afterwards
- **THEN** the listing SHALL reflect that change rather than a cached earlier state, so a restorable entry is never hidden and a listed entry is never already gone

### Requirement: Emptying the trash

CyberFS SHALL let an owner destroy the entries in their own trash in one call. Because the operation is irreversible and affects many nodes, the caller SHALL state how many entries the trash currently holds, the call SHALL be refused if it holds a different number, and the number of nodes one call destroys SHALL be bounded — save that a single entry SHALL always be destroyable in one call, however large it is, so that no trash can become impossible to empty.

#### Scenario: Emptying the trash destroys every entry and frees the space

- **WHEN** the owner empties a trash holding several entries within the node bound, stating the number of entries it holds
- **THEN** the system SHALL purge each entry and its subtree — metadata, every version's stored object, wrapped data keys, grants, and public links — and SHALL release the reclaimed bytes from the owner's quota immediately

#### Scenario: A count that does not match refuses the whole operation

- **WHEN** the stated number of entries differs from the number the trash currently holds
- **THEN** the system SHALL respond `409 Conflict` with an error code identifying the count as stale, SHALL destroy nothing, and SHALL leave the quota unchanged

#### Scenario: A live node is never destroyed

- **WHEN** the trash is emptied
- **THEN** only nodes already soft-deleted SHALL be destroyed, and no live node SHALL be affected even if it sits beneath a folder that has trashed siblings

#### Scenario: Only the caller's own trash

- **WHEN** any caller, administrator included, empties a trash
- **THEN** the trash emptied SHALL be their own, and there SHALL be no parameter naming another user, because destroying another user's trash wholesale is not offered — per-node purge remains the administrative path

#### Scenario: One call destroys a bounded number of nodes

- **WHEN** the trash holds more nodes than one call may destroy
- **THEN** the system SHALL take entries oldest deletion first and SHALL stop before starting an entry that would exceed the node bound, and SHALL report how many entries remain so the caller can continue with the count the response reports

#### Scenario: No entry is left partly destroyed

- **WHEN** a call stops because the node bound is reached
- **THEN** every entry it touched SHALL have been destroyed completely, and no entry SHALL remain listed with part of its subtree already destroyed

#### Scenario: An entry larger than the bound is still destroyed

- **WHEN** the oldest entry alone holds more nodes than the bound allows
- **THEN** the system SHALL destroy that entry rather than refusing, because otherwise no sequence of calls could ever empty the trash

#### Scenario: Emptying an already empty trash

- **WHEN** the owner empties a trash holding nothing, stating zero entries
- **THEN** the system SHALL succeed having destroyed nothing rather than reporting an error

#### Scenario: Emptying the trash is attributable

- **WHEN** the trash is emptied
- **THEN** each entry destroyed SHALL be recorded exactly as an individual purge of that entry records it, and one further record SHALL name the batch with its entry count and reclaimed bytes; both SHALL be security records, retained rather than pruned with activity records
