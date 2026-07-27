## MODIFIED Requirements

### Requirement: Listing, search, and metadata

CyberFS SHALL expose node metadata — identifier, name, type, parent, owner, size, content type, digest, encryption state, timestamps, tags, key/value metadata, and the caller's effective permission — and SHALL allow searching by name, tag, and metadata within the subtrees a caller may access.

#### Scenario: Effective permission reported

- **WHEN** a caller reads a node's metadata
- **THEN** the response SHALL include the caller's effective permission for that node

#### Scenario: Search scoped to accessible nodes

- **WHEN** a caller searches by name substring
- **THEN** results SHALL include only nodes the caller owns or has been granted, and SHALL never include nodes from other users' private subtrees

#### Scenario: Content is not searchable

- **WHEN** a caller searches
- **THEN** the system SHALL match on metadata only and SHALL NOT index or match file content

#### Scenario: The content digest is reported

- **WHEN** a caller who may read a file reads its metadata or lists its versions
- **THEN** the response SHALL carry the SHA-256 digest of the plaintext for the current version and for each listed version

#### Scenario: The digest is withheld from the administrative surface

- **WHEN** an administrator views any administrative report
- **THEN** no digest SHALL appear, because a plaintext digest would let a holder test whether a user has a specific known file even though its content is encrypted

## ADDED Requirements

### Requirement: Tags

A node SHALL carry a set of short labels, independent of its name, that a caller may filter on.

#### Scenario: Tags are set and read back

- **WHEN** a caller with `EDITOR` replaces a node's tags
- **THEN** the node SHALL carry exactly those tags, and a caller with `VIEWER` SHALL see them

#### Scenario: Tags are a set, not a list

- **WHEN** tags are written with duplicates, or in a different order
- **THEN** the stored result SHALL be the same set, with each tag present once

#### Scenario: Tags are normalized

- **WHEN** a tag is written with differing case or surrounding whitespace
- **THEN** it SHALL be normalized so that searching for it matches regardless of how it was typed

#### Scenario: A tag that is empty or too long is refused

- **WHEN** a tag is blank, whitespace only, or longer than the permitted length
- **THEN** the system SHALL refuse the request and SHALL change nothing

#### Scenario: Too many tags are refused

- **WHEN** a write would leave a node with more tags than the permitted maximum
- **THEN** the system SHALL refuse the request and SHALL change nothing

#### Scenario: Writing tags requires edit permission

- **WHEN** a caller holding only `VIEWER` attempts to change tags
- **THEN** the system SHALL refuse and SHALL change nothing

#### Scenario: Changing tags is a node mutation

- **WHEN** a node's tags change
- **THEN** its revision SHALL advance, so a stale `If-Match` is refused, and the change SHALL be recorded in the caller's activity

#### Scenario: Tags do not survive the node

- **WHEN** a node is purged
- **THEN** its tags SHALL be removed with it

### Requirement: Key/value metadata

A node SHALL carry key/value string pairs, so an integration can record facts about it that are not expressible as a name or a tag.

#### Scenario: Metadata is set and read back

- **WHEN** a caller with `EDITOR` replaces a node's metadata
- **THEN** the node SHALL carry exactly those pairs, and a caller with `VIEWER` SHALL see them

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

#### Scenario: Writing metadata requires edit permission

- **WHEN** a caller holding only `VIEWER` attempts to change metadata
- **THEN** the system SHALL refuse and SHALL change nothing

#### Scenario: Metadata does not survive the node

- **WHEN** a node is purged
- **THEN** its metadata SHALL be removed with it

### Requirement: Searching by tag and metadata

Search SHALL accept tag and metadata filters alongside the name substring, under the same access scoping as the name search.

#### Scenario: Search by tag

- **WHEN** a caller searches with a tag filter
- **THEN** results SHALL be the accessible nodes carrying that tag

#### Scenario: Several tags narrow the result

- **WHEN** a caller searches with more than one tag
- **THEN** results SHALL include only nodes carrying every one of them

#### Scenario: Search by metadata key

- **WHEN** a caller searches with a metadata key and no value
- **THEN** results SHALL be the accessible nodes carrying that key, whatever its value

#### Scenario: Search by metadata key and value

- **WHEN** a caller searches with both a key and a value
- **THEN** results SHALL be the accessible nodes where that key holds exactly that value

#### Scenario: Filters combine

- **WHEN** a caller supplies a name substring together with tag or metadata filters
- **THEN** results SHALL satisfy all of them, each filter narrowing the result rather than widening it

#### Scenario: Tag and metadata search obeys the access scope

- **WHEN** a caller searches by tag or metadata
- **THEN** results SHALL include only nodes the caller owns or has been granted, on the same terms as the name search, and a node shared with nobody SHALL never appear for another caller

#### Scenario: A trashed node is not found

- **WHEN** a caller searches by name, tag, or metadata
- **THEN** trashed nodes SHALL NOT appear

#### Scenario: Results stay bounded

- **WHEN** a search matches more nodes than the permitted page size
- **THEN** the system SHALL return at most that many rather than scanning without limit
