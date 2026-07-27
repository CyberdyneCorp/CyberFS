## MODIFIED Requirements

### Requirement: Searching by tag and metadata

Search SHALL accept tag and metadata filters alongside the name substring, under the same access scoping as the name search. Tag filters SHALL combine with each other according to an explicit match mode — every tag by default, or any one of them on request — and SHALL combine with the name and metadata filters by narrowing in every mode.

#### Scenario: Search by tag

- **WHEN** a caller searches with a tag filter
- **THEN** results SHALL be the accessible nodes carrying that tag

#### Scenario: Several tags narrow the result

- **WHEN** a caller searches with more than one tag and does not request the any-of match mode
- **THEN** results SHALL include only nodes carrying every one of them

#### Scenario: Any one of several tags

- **WHEN** a caller searches with more than one tag and requests the any-of match mode
- **THEN** results SHALL include every accessible node carrying at least one of them, and each matching node SHALL appear once however many of the tags it carries

#### Scenario: The match mode governs only the tags

- **WHEN** a caller requests the any-of match mode together with a name substring or a metadata filter
- **THEN** the name and metadata filters SHALL still narrow the result, so the mode SHALL NOT loosen anything other than how the tags combine with each other

#### Scenario: More tags than a node may carry is refused

- **WHEN** a search names more tags than the permitted maximum per node
- **THEN** the system SHALL respond `422 Unprocessable Entity` in either match mode, rather than running a scan that cannot match in the all-of mode

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

#### Scenario: Results stay bounded, and the remainder stays reachable

- **WHEN** a search matches more nodes than the permitted page size
- **THEN** the system SHALL return at most that many rather than scanning without limit, and SHALL return a cursor with which the caller reaches the rest, so that bounding a page never makes a match unreachable

## ADDED Requirements

### Requirement: Paginated and ordered search results

Search SHALL return results in a deterministic total order and SHALL carry an opaque cursor whenever further matches exist, using the same cursor mechanism as folder listing, so that every match a caller is entitled to see is reachable. A cursor SHALL be valid only for the filter set it was issued for.

#### Scenario: A page beyond the first is reachable

- **WHEN** a search matches more nodes than the requested limit
- **THEN** the response SHALL carry a cursor, and presenting that cursor with the same filters SHALL return the following results, repeating until a response carries no cursor

#### Scenario: The last page carries no cursor

- **WHEN** a response contains the final match for its filters
- **THEN** it SHALL carry no cursor, so a caller SHALL be able to tell the walk is complete without issuing a further request

#### Scenario: Every match is returned exactly once

- **WHEN** a caller walks every page of a result set that does not change during the walk
- **THEN** each matching node SHALL appear exactly once across the pages, including nodes that share a name with another match

#### Scenario: The order is total and stated

- **WHEN** results are returned
- **THEN** they SHALL be ordered by the node's normalized name ascending in the database collation, with ties broken by the node identifier, and the order SHALL NOT group folders before files and SHALL NOT be ranked by relevance to the query

#### Scenario: The cursor and the order agree

- **WHEN** a cursor is presented
- **THEN** the results that follow SHALL be exactly those that sort after the last result of the previous page under that same order

#### Scenario: A cursor presented with different filters is refused

- **WHEN** a caller presents a cursor together with a filter set other than the one it was issued for, including a different tag match mode
- **THEN** the system SHALL respond `422 Unprocessable Entity` and SHALL return no results, rather than serving a page of a walk the cursor does not describe

#### Scenario: A malformed cursor is refused

- **WHEN** a cursor is not one the system issued
- **THEN** the system SHALL respond `422 Unprocessable Entity` and SHALL return no results

#### Scenario: The page size is bounded

- **WHEN** a caller requests a limit above `PAGE_SIZE_MAX`
- **THEN** the system SHALL return at most `PAGE_SIZE_MAX` results and SHALL still carry a cursor for the remainder

#### Scenario: A search without a filter is refused

- **WHEN** a caller supplies a cursor or a limit but no name, tag, or metadata filter
- **THEN** the system SHALL respond `422 Unprocessable Entity`, because paging through everything a caller can reach is what walking the tree is for

#### Scenario: Pagination does not widen the access scope

- **WHEN** a caller walks every page of a search
- **THEN** every result on every page SHALL be a node the caller owns or holds an active grant on, and SHALL NOT be trashed, on exactly the terms that apply to the first page

### Requirement: Tag discovery

CyberFS SHALL let a caller enumerate the tags in use across the nodes they may search, each with the number of those nodes carrying it, so that a caller discovers their own vocabulary rather than having to remember it.

#### Scenario: Tags are listed with their usage counts

- **WHEN** a caller requests their tag inventory
- **THEN** the response SHALL list each tag in use, in its normalized form, with the number of nodes in the caller's search scope carrying it

#### Scenario: The inventory obeys the search access scope

- **WHEN** a caller requests their tag inventory
- **THEN** it SHALL cover only nodes the caller owns or holds an active grant on, a tag used solely on another user's unshared nodes SHALL NOT appear, and a pending grant SHALL contribute nothing

#### Scenario: Trashed nodes are excluded from the inventory

- **WHEN** a node carrying a tag is trashed
- **THEN** it SHALL stop contributing to that tag's count, and if it was the last carrier in scope the tag SHALL NOT appear at all, so no tag is ever reported with a count of zero

#### Scenario: The inventory agrees with search

- **WHEN** the inventory reports a tag with a count of `n`
- **THEN** searching with that tag as the only filter SHALL return exactly those `n` nodes, across as many pages as the page bound requires

#### Scenario: Counts are per caller, not global

- **WHEN** two callers with different access to the same tagged nodes request the inventory
- **THEN** each count SHALL reflect only that caller's scope, so the number SHALL NOT be reported or interpreted as a property of the tag itself

#### Scenario: The inventory is ordered and paginated

- **WHEN** more tags are in use than the requested limit
- **THEN** the response SHALL list them ordered by tag ascending, SHALL carry a cursor for the remainder, and walking every page SHALL report each tag exactly once

#### Scenario: The inventory is narrowed by prefix

- **WHEN** a caller supplies a prefix
- **THEN** the response SHALL list only tags beginning with it, matched against the normalized tag form so that the case and surrounding whitespace of the prefix do not affect the result

#### Scenario: An inventory cursor is bound to its prefix

- **WHEN** a caller presents an inventory cursor together with a different prefix
- **THEN** the system SHALL respond `422 Unprocessable Entity`, on the same terms as a search cursor presented with different filters

#### Scenario: The inventory requires an authenticated caller

- **WHEN** an unauthenticated request asks for a tag inventory
- **THEN** the system SHALL respond `401 Unauthorized` and SHALL disclose no tag
