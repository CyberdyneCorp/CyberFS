## MODIFIED Requirements

### Requirement: S3 access keys are a credential, not an identity

CyberFS SHALL issue long-lived access keys that authenticate a request as an existing subject, for clients that cannot perform an interactive OAuth flow. A key SHALL confer no permission of its own: it names a subject, and that subject's grants decide what the request may do. Keys SHALL serve every such surface — S3 signature verification and WebDAV Basic authentication alike — rather than being specific to one.

#### Scenario: A key names a subject

- **WHEN** a request authenticates with an access key
- **THEN** it SHALL be treated as that key's subject, with exactly the permissions that subject holds

#### Scenario: A key carries no permission of its own

- **WHEN** a subject's grants change
- **THEN** what their access keys may do SHALL change with them, without the keys being reissued

#### Scenario: Revocation is immediate

- **WHEN** a key is revoked
- **THEN** the next request presenting it SHALL be refused, with no cached decision to expire

#### Scenario: The secret is never recoverable from storage alone

- **WHEN** an access key is stored
- **THEN** its secret SHALL be sealed under `MASTER_KEY`, so a database compromise alone does not yield a usable credential

#### Scenario: One key works across surfaces

- **WHEN** a subject holds an active access key
- **THEN** it SHALL authenticate on every surface that accepts access keys, so a client is not made to hold a different credential per protocol
