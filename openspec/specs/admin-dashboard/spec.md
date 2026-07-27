# admin-dashboard Specification

## Purpose
TBD - created by archiving change bootstrap-cyberfs. Update Purpose after archive.
## Requirements
### Requirement: Admin surface is metadata-only

The admin API and dashboard SHALL expose only metadata and aggregates. No administrative endpoint SHALL return file content, previews, thumbnails, extracted text, or key material, for encrypted or unencrypted files alike.

#### Scenario: No content endpoint on the admin API

- **WHEN** the admin API surface is enumerated
- **THEN** it SHALL contain no route that streams or returns node content

#### Scenario: Admin browsing a user's tree

- **WHEN** an administrator inspects a user's storage
- **THEN** the response SHALL include node identifiers, types, sizes, content types, encryption state, timestamps, and share counts, and SHALL NOT include content

#### Scenario: File names redacted by default

- **WHEN** an administrator lists another user's nodes
- **THEN** names SHALL be omitted unless `ADMIN_SHOW_FILENAMES` is enabled, and enabling it SHALL be recorded in the audit log

#### Scenario: Admin cannot escalate to content

- **WHEN** an administrator calls a non-admin content endpoint for a file they do not own or hold a grant on
- **THEN** the system SHALL respond `403 Forbidden`

### Requirement: Access to the admin surface

Every admin route SHALL require an effective `is_admin` principal, verified through CyberdyneAuth introspection rather than the JWT claim alone.

#### Scenario: Non-admin denied

- **WHEN** an authenticated non-admin requests any admin route
- **THEN** the system SHALL respond `403 Forbidden`

#### Scenario: Demoted admin denied immediately

- **WHEN** an administrator is demoted at CyberdyneAuth while holding an unexpired token
- **THEN** their next admin request SHALL be denied without waiting for the token to expire

#### Scenario: Admin actions audited

- **WHEN** an administrator performs any state-changing action
- **THEN** an audit record SHALL capture the actor subject, action, target, parameters, and timestamp

### Requirement: Per-user storage statistics

The dashboard SHALL report, for each user: bytes used, quota, percentage consumed, file count, folder count, count and bytes of encrypted versus unencrypted files, bytes held in trash, bytes held in retained versions, share counts granted and received, last activity time, and account creation time.

#### Scenario: Usage breakdown shown

- **WHEN** an administrator opens a user's detail view
- **THEN** it SHALL show live bytes, trashed bytes, and version bytes as distinct figures summing to the charged usage

#### Scenario: Encryption adoption shown

- **WHEN** a user's detail view is rendered
- **THEN** it SHALL show what share of that user's files and bytes are encrypted

#### Scenario: Users sortable and filterable

- **WHEN** an administrator views the user list
- **THEN** it SHALL be sortable by usage, quota percentage, file count, and last activity, and filterable by over-quota and inactive states

#### Scenario: Figures reconcile with storage

- **WHEN** the reported total bytes are compared against the sum of node sizes in Postgres
- **THEN** they SHALL agree, and any drift SHALL be corrected by the reconciliation job rather than displayed indefinitely

### Requirement: Tenant-wide statistics

The dashboard SHALL report system-wide totals: total bytes stored, total files and folders, per-content-type distribution, encrypted share of storage, growth over time, top consumers, active users over a window, and public-link counts.

#### Scenario: Growth trend rendered

- **WHEN** an administrator opens the overview
- **THEN** it SHALL chart total stored bytes and file count over a selectable window of 7, 30, or 90 days

#### Scenario: Top consumers listed

- **WHEN** the overview is rendered
- **THEN** it SHALL list the top N users by bytes with their quota percentage

#### Scenario: Aggregates computed without reading content

- **WHEN** any statistic is produced
- **THEN** it SHALL be derived from metadata only

### Requirement: Quota administration

Administrators SHALL be able to view and change any user's quota, and the change SHALL take effect on the next quota check.

#### Scenario: Quota raised

- **WHEN** an administrator raises a user's quota
- **THEN** subsequent uploads up to the new limit SHALL be accepted

#### Scenario: Quota lowered below current usage

- **WHEN** an administrator sets a quota below the user's current usage
- **THEN** the system SHALL accept the change, mark the user over quota, block new uploads, and continue to permit reads and deletions

#### Scenario: Quota change audited

- **WHEN** a quota is changed
- **THEN** the audit record SHALL contain the previous and new values

#### Scenario: Non-admin cannot change quota

- **WHEN** a non-admin attempts to change any quota, including their own
- **THEN** the system SHALL respond `403 Forbidden`

### Requirement: Share and activity auditing views

The dashboard SHALL let administrators review sharing posture and recent activity across the deployment.

#### Scenario: Public links reviewed

- **WHEN** an administrator opens the sharing view
- **THEN** it SHALL list active public links with owner, target type, creation time, expiry, passphrase protection state, and access count

#### Scenario: Public link revoked by admin

- **WHEN** an administrator revokes a public link
- **THEN** the link SHALL stop working immediately and the action SHALL be audited

#### Scenario: Audit log browsable

- **WHEN** an administrator opens the audit view
- **THEN** it SHALL be filterable by actor, action type, target, and time range, with pagination

#### Scenario: Admin cannot revoke user-to-user grants

- **WHEN** an administrator attempts to revoke a grant between two users
- **THEN** the system SHALL respond `403 Forbidden`, since grant management belongs to the node owner

### Requirement: Operational health view

The dashboard SHALL surface the health of CyberFS's dependencies and background jobs.

#### Scenario: Dependency status shown

- **WHEN** an administrator opens the health view
- **THEN** it SHALL show the reachability and latency of Postgres, Redis, MinIO, and CyberdyneAuth

#### Scenario: Job status shown

- **WHEN** the health view is rendered
- **THEN** it SHALL show the last run, outcome, and duration of the purge, orphan reaper, reconciliation, and backup jobs

#### Scenario: Degraded cache visible

- **WHEN** Redis is unreachable
- **THEN** the health view SHALL show the cache as degraded while the rest of the dashboard continues to function

### Requirement: Dashboard architecture

The dashboard SHALL be a SvelteKit 2 / Svelte 5 application following MVVM: each route SHALL have a view model in a `*.vm.svelte.ts` module holding state and behaviour, and `.svelte` components SHALL contain presentation only. View models SHALL be unit-testable without mounting components.

#### Scenario: View models carry the logic

- **WHEN** a route's data loading, filtering, sorting, pagination, or error handling is implemented
- **THEN** it SHALL live in that route's view model, not in the `.svelte` component

#### Scenario: Components hold no API calls

- **WHEN** a `.svelte` component is reviewed
- **THEN** it SHALL contain no direct HTTP calls and no business rules

#### Scenario: View models tested headlessly

- **WHEN** the dashboard test suite runs
- **THEN** every view model SHALL be exercised against a mocked API client without a DOM

#### Scenario: API client is the only network boundary

- **WHEN** the dashboard calls the backend
- **THEN** it SHALL do so through a single typed API client module

### Requirement: Dashboard access and session behaviour

The dashboard SHALL authenticate through CyberdyneAuth and SHALL not be reachable by non-admins.

#### Scenario: Unauthenticated visitor redirected

- **WHEN** an unauthenticated visitor opens any dashboard route
- **THEN** they SHALL be redirected to the dashboard's sign-in page, which SHALL offer both the CyberdyneAuth OAuth flow and password sign-in, and SHALL be returned to the requested route after either succeeds

#### Scenario: Authenticated non-admin refused

- **WHEN** an authenticated non-admin opens the dashboard
- **THEN** they SHALL see an access-denied page and no statistics

#### Scenario: Expired session recovered

- **WHEN** the access token expires while the dashboard is open
- **THEN** the client SHALL refresh it transparently, and SHALL redirect to login only if refresh fails

#### Scenario: Dashboard is accessible

- **WHEN** the dashboard's automated accessibility checks run
- **THEN** every route SHALL pass with no serious or critical violations

### Requirement: Password sign-in

The dashboard SHALL offer email/password sign-in alongside the OAuth button, and
SHALL obtain its session from CyberdyneAuth in both cases. The dashboard SHALL NOT
verify credentials itself, SHALL NOT decide administrator status itself, and SHALL
NOT persist the password or any second-factor code.

#### Scenario: Password sign-in succeeds

- **WHEN** an operator submits a correct email and password for an account without a second factor
- **THEN** the dashboard SHALL adopt the returned access and refresh tokens, resolve the profile through CyberdyneAuth, and admit the operator only if that profile reports administrator status

#### Scenario: Second factor requested

- **WHEN** CyberdyneAuth answers a password submission with an MFA challenge rather than tokens
- **THEN** the dashboard SHALL prompt for a one-time code and complete the sign-in by presenting that code together with the challenge token

#### Scenario: Second factor rejected

- **WHEN** the operator submits an incorrect or expired one-time code
- **THEN** the dashboard SHALL report the failure, SHALL keep the operator on the code prompt, and SHALL NOT admit them

#### Scenario: Failed sign-in does not reveal whether the account exists

- **WHEN** sign-in fails because no such account exists, or because the password is wrong
- **THEN** the dashboard SHALL show the same message in both cases

#### Scenario: Rate limiting is surfaced

- **WHEN** CyberdyneAuth refuses a sign-in attempt because too many have been made
- **THEN** the dashboard SHALL say so specifically, rather than reporting it as a wrong password

#### Scenario: Authenticated non-admin refused after password sign-in

- **WHEN** an operator signs in with a valid password but the profile reports they are not an administrator
- **THEN** the dashboard SHALL refuse access on the same terms as any other non-admin

#### Scenario: Credentials are not retained

- **WHEN** a sign-in attempt completes, whether it succeeded or failed
- **THEN** the password and any one-time code SHALL NOT be present in session storage, local storage, the URL, or any log

