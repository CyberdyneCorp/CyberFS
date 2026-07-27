## ADDED Requirements

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

## MODIFIED Requirements

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
