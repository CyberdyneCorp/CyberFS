## ADDED Requirements

### Requirement: File operations are recorded

CyberFS SHALL record an audit entry for every operation a user performs on a
node: upload, download, create, rename, move, copy, delete, restore, and version
restore. Each entry SHALL carry the acting subject, the node, the operation, the
protocol used, the byte count where one applies, and the time.

#### Scenario: An upload is recorded

- **WHEN** a user uploads a file
- **THEN** an entry SHALL be written identifying them as the actor, the node, and
  the number of plaintext bytes stored

#### Scenario: A download is recorded

- **WHEN** a user downloads a file
- **THEN** an entry SHALL be written, so an owner can see that their content was
  read

#### Scenario: A read of someone else's file names the reader

- **WHEN** a share recipient downloads a file
- **THEN** the entry SHALL identify the recipient as the actor and the owner's
  node as the target

#### Scenario: A public-link read is attributed to the link

- **WHEN** content is fetched through a public link
- **THEN** the entry SHALL identify the link rather than a user, since there is
  no authenticated caller

#### Scenario: A refused operation is not recorded as activity

- **WHEN** an operation is denied for lack of permission
- **THEN** it SHALL be recorded as an authorization denial and SHALL NOT appear in
  the actor's activity as though it succeeded

#### Scenario: Recording never holds content

- **WHEN** any operation entry is written
- **THEN** it SHALL NOT contain file content, and SHALL NOT contain the file name
  unless the actor owns the node

### Requirement: Activity summary

CyberFS SHALL expose an endpoint returning the calling user's own activity over
a chosen window: counts per operation, bytes uploaded and downloaded, and the
busiest day.

#### Scenario: Totals are returned for the window

- **WHEN** a user requests their activity for the last 30 days
- **THEN** the response SHALL carry counts of uploads, downloads, shares granted,
  shares revoked, deletions, and restores within that window

#### Scenario: Public links count toward the share totals

- **WHEN** a user issues or revokes a public link
- **THEN** it SHALL count toward shares granted or shares revoked respectively, so
  the summary never reports zero shares while the feed lists links the user created

#### Scenario: Byte totals are plaintext bytes

- **WHEN** the summary reports bytes uploaded or downloaded
- **THEN** the figures SHALL be plaintext sizes, matching what the user actually
  sent or received

#### Scenario: The window is bounded

- **WHEN** a user requests a window longer than `ACTIVITY_MAX_WINDOW_DAYS`
- **THEN** the system SHALL respond `422`, rather than scanning an unbounded range

#### Scenario: A quiet account returns zeroes

- **WHEN** a user with no activity requests a summary
- **THEN** the system SHALL return zero counts rather than an error

#### Scenario: Activity as a recipient is included

- **WHEN** a user downloads a file shared with them
- **THEN** that download SHALL count toward their own activity

### Requirement: Activity feed

The endpoint SHALL also return a chronological, paginated feed of the individual
operations behind the summary, newest first.

#### Scenario: The feed lists individual operations

- **WHEN** a user requests their activity
- **THEN** the response SHALL include recent operations, each carrying the
  action, the time, and the node it concerned

#### Scenario: The feed paginates

- **WHEN** more operations exist than the page size
- **THEN** the response SHALL carry a cursor that returns the remainder, and the
  pages SHALL not overlap

#### Scenario: The feed can be filtered by action

- **WHEN** a user requests only downloads
- **THEN** the feed SHALL contain only download entries, while the summary
  totals SHALL remain unfiltered

#### Scenario: Names appear only for nodes the user owns

- **WHEN** the feed includes an operation on a file owned by someone else
- **THEN** the entry SHALL identify the node by id and SHALL NOT carry its name

#### Scenario: Purged nodes remain in the feed

- **WHEN** a node named by an entry has since been purged
- **THEN** the entry SHALL still be returned, identifying the node by id, since
  history must not vanish when its subject does

### Requirement: One user's activity is private to them

The endpoint SHALL return only the calling user's own operations. There SHALL be
no parameter by which a caller can request another user's activity.

#### Scenario: The endpoint is self-scoped

- **WHEN** any caller requests activity
- **THEN** the system SHALL return operations where they were the actor, and no
  others

#### Scenario: Another user's activity cannot be requested

- **WHEN** a caller supplies a subject or user identifier alongside the request
- **THEN** the system SHALL ignore it and return the caller's own activity

#### Scenario: An owner does not see a recipient's activity here

- **WHEN** a share recipient reads an owner's file
- **THEN** that read SHALL appear in the recipient's activity, and the owner
  SHALL see it through the audit log rather than through this endpoint

#### Scenario: An administrator uses the audit log, not this endpoint

- **WHEN** an administrator wants another user's activity
- **THEN** they SHALL use the admin audit surface, which is already access
  controlled and already audited

#### Scenario: A service principal has no activity

- **WHEN** a service principal requests activity
- **THEN** the system SHALL refuse, since a service owns no tree

### Requirement: Activity data is retained and bounded

Operation records SHALL be retained for a configurable window and pruned
afterwards, and the tables behind them SHALL be indexed for the queries this
capability makes.

#### Scenario: Records are pruned after their retention

- **WHEN** an operation record is older than `ACTIVITY_RETENTION_DAYS`
- **THEN** a prune job SHALL delete it

#### Scenario: Pruning does not touch security records

- **WHEN** the prune job runs
- **THEN** authorization denials, grant changes, ownership transfers, encryption
  changes, and administrative actions SHALL be retained under the longer audit
  retention, since they are evidence rather than activity

#### Scenario: The summary query is bounded

- **WHEN** a summary is computed
- **THEN** it SHALL be answerable from an index on actor and time, not a full
  scan

#### Scenario: Recording never fails the operation

- **WHEN** writing an activity record fails
- **THEN** the underlying file operation SHALL still succeed, and the failure
  SHALL be logged at error level -- losing a log line must not lose a user's file
