## MODIFIED Requirements

### Requirement: Key/value metadata

A node SHALL carry key/value string pairs, so an integration can record facts about it that are not expressible as a name or a tag. Pairs whose key lies in the namespace reserved for system use SHALL NOT be writable or removable by a caller, SHALL survive any write a caller makes, and SHALL NOT appear in any response to a caller.

#### Scenario: Metadata is set and read back

- **WHEN** a caller with `EDITOR` replaces a node's metadata
- **THEN** the node SHALL carry exactly those pairs among the keys a caller may write, and a caller with `VIEWER` SHALL see them

#### Scenario: A key appears once

- **WHEN** metadata is written containing the same key twice
- **THEN** the system SHALL refuse the request rather than silently keeping one of the values

#### Scenario: Oversized keys or values are refused

- **WHEN** a key is empty or over the permitted length, or a value is over the permitted length
- **THEN** the system SHALL refuse the request and SHALL change nothing

#### Scenario: Too many pairs are refused

- **WHEN** a write would leave a node with more pairs than the permitted maximum
- **THEN** the system SHALL refuse the request and SHALL change nothing

#### Scenario: The reserved namespace is protected

- **WHEN** a caller writes a key in the namespace reserved for system use
- **THEN** the system SHALL refuse the request, so system-written metadata can never be forged by a caller

#### Scenario: The reserved namespace survives a replace

- **WHEN** a caller replaces a node's metadata, including with an empty collection, while a pair in the reserved namespace exists on that node
- **THEN** the reserved pair SHALL remain, so a caller cannot clear system-written metadata by replacing the metadata they can write

#### Scenario: Reserved metadata is not shown to a caller

- **WHEN** a caller reads a node, or writes its metadata, while a pair in the reserved namespace exists on that node
- **THEN** the metadata in the response SHALL omit that pair, so the metadata a caller is handed is exactly the metadata it may write back

#### Scenario: The reserved-namespace test ignores case

- **WHEN** a caller writes or removes a key whose prefix matches the reserved namespace in any letter case
- **THEN** the system SHALL refuse the request, so the namespace cannot be reached by recasing its prefix

#### Scenario: Writing metadata requires edit permission

- **WHEN** a caller holding only `VIEWER` attempts to change metadata
- **THEN** the system SHALL refuse and SHALL change nothing

#### Scenario: Metadata does not survive the node

- **WHEN** a node is purged
- **THEN** its metadata SHALL be removed with it

## ADDED Requirements

### Requirement: Partial label updates

CyberFS SHALL allow a caller with `EDITOR` to add and remove individual tags, and to set and delete individual metadata keys, in one request that merges with what the node already carries rather than replacing it. A partial update SHALL name what it adds and what it removes explicitly, and SHALL NOT rely on any in-band value meaning removal. A partial update SHALL NOT alter any label it does not name. Partial updates to one node SHALL be applied one at a time, so that both the maximum a node may hold and the determination that an update changes nothing are decided against a state no other update can alter before the update lands. Concurrent partial updates to one node SHALL therefore be ordered rather than simultaneous, and each SHALL see the effect of those ordered ahead of it.

#### Scenario: Tags are added and removed together

- **WHEN** a caller with `EDITOR` submits a partial tag update naming tags to add and tags to remove
- **THEN** the node SHALL afterwards carry its previous tags plus the added ones and minus the removed ones, and the response SHALL report the resulting set

#### Scenario: Metadata keys are set and deleted together

- **WHEN** a caller with `EDITOR` submits a partial metadata update naming pairs to set and keys to remove
- **THEN** the named keys SHALL hold the supplied values, the named removals SHALL be gone, and every key the request did not name SHALL be unchanged

#### Scenario: Concurrent partial updates do not lose each other

- **WHEN** two callers concurrently add different tags to the same node, neither supplying a precondition
- **THEN** the node SHALL afterwards carry both tags, and neither update SHALL have been overwritten by the other

#### Scenario: Concurrent partial updates advance the revision separately

- **WHEN** two partial updates that both change a node's labels are applied concurrently
- **THEN** each SHALL advance the node's revision, so no two distinct label states SHALL share a validator

#### Scenario: A partial update that changes nothing writes nothing

- **WHEN** a partial update only adds tags the node already carries, or only removes tags or keys it does not have
- **THEN** the system SHALL report success with the node's current labels, and SHALL NOT advance the revision, SHALL NOT record activity, and SHALL NOT invalidate any cached view

#### Scenario: A partial update that changes something is a node mutation

- **WHEN** a partial update alters a node's tags or metadata
- **THEN** the node's revision SHALL advance and the change SHALL be recorded in the caller's activity, on the same terms as replacing the collection

#### Scenario: A stale precondition is refused even when the update would change nothing

- **WHEN** a caller submits a partial update with an `If-Match` token that no longer matches the node
- **THEN** the system SHALL respond `412 Precondition Failed` and SHALL change nothing, whether or not the update would have had any effect

#### Scenario: A contradictory partial update is refused

- **WHEN** a partial update names the same tag as both an addition and a removal, or the same metadata key as both a set and a removal
- **THEN** the system SHALL refuse the request and SHALL change nothing, rather than choosing an order

#### Scenario: An empty partial update is refused

- **WHEN** a partial update names nothing to add, set, or remove
- **THEN** the system SHALL refuse the request, because reading a node's labels is what reading a node is for

#### Scenario: Limits are enforced against the merged result

- **WHEN** a partial update would leave a node with more tags or more metadata pairs than the permitted maximum
- **THEN** the system SHALL refuse the request and SHALL change nothing, exactly as replacing the collection would

#### Scenario: Two concurrent partial updates cannot jointly exceed the maximum

- **WHEN** two partial updates are applied concurrently to a node near the permitted maximum, and each would fit on its own but together they would not
- **THEN** one SHALL succeed and the other SHALL be refused for exceeding the maximum, and the node SHALL never hold more than the maximum

#### Scenario: A successful partial update returns the node's new validator

- **WHEN** a partial update changes a node's labels
- **THEN** the response SHALL carry the node's post-update ETag, which SHALL equal the ETag a subsequent read returns and SHALL be accepted as the `If-Match` of the next update

#### Scenario: A partial update on a trashed node is refused

- **WHEN** a caller submits a partial update naming a node that is in the trash
- **THEN** the system SHALL respond `404 Not Found` and SHALL change nothing, as renaming or moving a trashed node does

#### Scenario: A removal names the stored form of a tag

- **WHEN** a partial update removes a tag written with different case or surrounding whitespace from how it was stored
- **THEN** the tag SHALL be removed, because normalization applies to a removal as it does to a write

#### Scenario: A partial update may not remove a reserved key

- **WHEN** a partial update names a metadata key in the namespace reserved for system use as a removal
- **THEN** the system SHALL refuse the request and SHALL change nothing

#### Scenario: A partial update requires edit permission

- **WHEN** a caller holding only `VIEWER` submits a partial tag or metadata update
- **THEN** the system SHALL refuse and SHALL change nothing

#### Scenario: Partial and whole-collection writes coexist

- **WHEN** a caller replaces a node's tags after another caller has patched them
- **THEN** the replacement SHALL win outright for every tag it does not name, because replacing states a complete collection while patching states a change to one
