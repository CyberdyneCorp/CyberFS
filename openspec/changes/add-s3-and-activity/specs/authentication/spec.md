## ADDED Requirements

### Requirement: S3 access keys are a credential, not an identity

CyberFS SHALL support access-key credentials that resolve to an existing
CyberdyneAuth subject. An access key SHALL NOT create a principal, SHALL NOT
carry permissions of its own, and SHALL NOT outlive the account it belongs to.

#### Scenario: A key resolves to its owner's subject

- **WHEN** a request is authenticated with an access key
- **THEN** the resulting principal SHALL be the same one a bearer token for that
  user would produce, including their org claims

#### Scenario: Admin status does not travel with a key

- **WHEN** an administrator authenticates with an access key
- **THEN** the resulting principal SHALL NOT be treated as an administrator,
  because a long-lived key is exactly the credential most likely to leak and
  administrative actions require a freshly introspected token

#### Scenario: Administrative routes reject key authentication

- **WHEN** a request signed with an access key targets an admin route
- **THEN** the system SHALL respond `403`

#### Scenario: A key for a deactivated account stops working

- **WHEN** the owning subject is no longer active at CyberdyneAuth
- **THEN** requests signed with their keys SHALL be refused

#### Scenario: Key material is stored irreversibly

- **WHEN** an access key is created
- **THEN** the secret SHALL be stored only as a verifier from which it cannot be
  recovered, and SHALL never appear in a log, a metric, or an API response after
  creation

#### Scenario: Revocation is immediate and needs no cache expiry

- **WHEN** an access key is revoked
- **THEN** the next request signed with it SHALL be refused

### Requirement: Freshness rules still apply

The split between claim-based and introspection-backed verification SHALL hold
regardless of which credential established the caller.

#### Scenario: Key-authenticated grants still introspect

- **WHEN** a caller authenticated by an access key performs a grant, a
  revocation, or an ownership transfer
- **THEN** the system SHALL verify the owning subject against CyberdyneAuth
  before acting, exactly as it would for a bearer token

#### Scenario: An identity-plane outage fails those operations closed

- **WHEN** CyberdyneAuth is unreachable during a revocation-sensitive operation
  authenticated by an access key
- **THEN** the system SHALL respond `503` rather than proceeding on the strength
  of the key alone

#### Scenario: Ordinary reads do not require a round trip

- **WHEN** a caller authenticated by an access key downloads a file
- **THEN** the system SHALL authorize locally, without introspecting
