## ADDED Requirements

### Requirement: Discovery-driven token verification

CyberFS SHALL act as an OAuth2/OIDC resource server for CyberdyneAuth and SHALL derive the issuer, JWKS URI, and accepted signing algorithms from the CyberdyneAuth discovery document at `${CYBERDYNE_AUTH_BASE_URL}/.well-known/openid-configuration`. CyberFS SHALL NOT hard-code the issuer string, the JWKS URL, or the signing algorithm.

#### Scenario: Valid access token accepted

- **WHEN** a request arrives with `Authorization: Bearer <token>` whose signature verifies against a key from the discovered `jwks_uri`, whose `iss` equals the discovered `issuer`, and whose `exp` is in the future
- **THEN** the system SHALL resolve the caller principal from the token claims and process the request

#### Scenario: Issuer mismatch rejected

- **WHEN** a token's `iss` claim does not equal the `issuer` published by the discovery document
- **THEN** the system SHALL respond `401 Unauthorized` and SHALL NOT process the request

#### Scenario: Unknown key id triggers JWKS refresh

- **WHEN** a token's header `kid` is absent from the cached JWKS
- **THEN** the system SHALL refetch the JWKS from the discovered `jwks_uri` at most once per `JWKS_REFRESH_COOLDOWN_SECONDS` before rejecting the token

#### Scenario: Unsigned token rejected

- **WHEN** a token presents `alg: none`, or an algorithm absent from the discovered algorithm list
- **THEN** the system SHALL respond `401 Unauthorized`

#### Scenario: Expired token rejected

- **WHEN** a token's `exp` claim is in the past, allowing at most 60 seconds of clock skew
- **THEN** the system SHALL respond `401 Unauthorized` with error code `token_expired`

#### Scenario: Missing credentials rejected

- **WHEN** a request to a protected endpoint carries no `Authorization` header and no session cookie
- **THEN** the system SHALL respond `401 Unauthorized` and SHALL NOT disclose whether the requested resource exists

### Requirement: Discovery and JWKS resilience

CyberFS SHALL cache the discovery document and JWKS and SHALL remain able to verify tokens while CyberdyneAuth is briefly unreachable.

#### Scenario: Discovery cached across requests

- **WHEN** the discovery document has been fetched successfully
- **THEN** the system SHALL reuse it for `OIDC_DISCOVERY_TTL_SECONDS` without refetching

#### Scenario: Auth service unavailable but cache warm

- **WHEN** CyberdyneAuth is unreachable and a cached JWKS is present and not older than `JWKS_STALE_MAX_SECONDS`
- **THEN** the system SHALL continue verifying tokens against the cached JWKS

#### Scenario: Auth service unavailable and cache cold

- **WHEN** CyberdyneAuth is unreachable and no usable cached JWKS exists
- **THEN** the system SHALL respond `503 Service Unavailable` with `Retry-After` and SHALL log the failure at error level

### Requirement: Introspection for revocation-sensitive operations

For operations whose authorization outcome must reflect the current state of the identity plane — administrative actions, permission grants, permission revocations, and ownership transfer — CyberFS SHALL verify the caller through RFC 7662 introspection against CyberdyneAuth rather than trusting the JWT claim alone.

#### Scenario: Revoked token blocked from an admin action

- **WHEN** a caller invokes an admin endpoint with a structurally valid, unexpired JWT that introspection reports as `active: false`
- **THEN** the system SHALL respond `401 Unauthorized`

#### Scenario: Demoted admin blocked

- **WHEN** a caller's JWT carries `is_admin: true` but introspection reports `is_admin: false`
- **THEN** the system SHALL respond `403 Forbidden` and SHALL treat the introspection result as authoritative

#### Scenario: Ordinary read uses the local claim

- **WHEN** a caller lists a folder or downloads a file
- **THEN** the system SHALL authorize from verified JWT claims without calling introspection

#### Scenario: Introspection outage on an admin action

- **WHEN** introspection cannot be reached during a revocation-sensitive operation
- **THEN** the system SHALL fail closed with `503 Service Unavailable` rather than falling back to the JWT claim

### Requirement: Service-to-service authentication

CyberFS SHALL authenticate to CyberdyneAuth using an OAuth2 client-credentials service client identified by `CYBERFS_CLIENT_ID` / `CYBERFS_CLIENT_SECRET`, and SHALL accept client-credentials tokens from other Cyberdyne services acting on their own behalf.

#### Scenario: Service token obtained and reused

- **WHEN** CyberFS needs a service token for introspection and no unexpired token is cached
- **THEN** the system SHALL request one via client credentials and SHALL cache it until 60 seconds before its expiry

#### Scenario: Service caller has no user identity

- **WHEN** a request is authenticated by a client-credentials token that carries no `sub` for a human user
- **THEN** the system SHALL treat the caller as a service principal and SHALL deny any operation that requires a file owner or share recipient

### Requirement: Principal resolution and first-touch provisioning

CyberFS SHALL identify every caller by the CyberdyneAuth `sub` claim and SHALL maintain a local user record keyed by that `sub`, created on the caller's first authenticated request.

#### Scenario: New user provisioned on first request

- **WHEN** an authenticated caller whose `sub` has no local record makes any request
- **THEN** the system SHALL create a local user record carrying `sub`, the org claims, a root folder, and the default quota, before processing the request

#### Scenario: Local record refreshed from claims

- **WHEN** an authenticated caller whose local record exists makes a request and the token's `org`, `orgs`, or `is_admin` claims differ from the stored values
- **THEN** the system SHALL update the stored values from the token

#### Scenario: Subject identity is stable across email change

- **WHEN** a caller's email address changes at CyberdyneAuth while `sub` is unchanged
- **THEN** the system SHALL continue to resolve the same local user and the same file ownership

#### Scenario: Missing orgs claim is not treated as all orgs

- **WHEN** a token omits the `orgs` claim entirely
- **THEN** the system SHALL treat the caller as having no org-scoped access rather than access to every org

### Requirement: Administrator authorization

CyberFS SHALL treat the `is_admin` signal from CyberdyneAuth as the sole source of administrative privilege and SHALL NOT maintain an independent admin role table.

#### Scenario: Non-admin denied admin endpoint

- **WHEN** a caller without effective `is_admin` requests any `/api/v1/admin/**` endpoint
- **THEN** the system SHALL respond `403 Forbidden`

#### Scenario: Admin privilege does not grant content access

- **WHEN** a caller with `is_admin: true` requests the content of a file they neither own nor have been granted
- **THEN** the system SHALL respond `403 Forbidden` regardless of whether the file is encrypted

### Requirement: Authorization failures are auditable

CyberFS SHALL record every authentication and authorization failure with the caller subject (when resolvable), the target resource id, the reason code, and the source IP, and SHALL NOT record token values or file content in logs.

#### Scenario: Denial recorded

- **WHEN** a request is rejected with `401` or `403`
- **THEN** the system SHALL emit an audit record containing the reason code and SHALL NOT include the bearer token

#### Scenario: Repeated denials rate limited

- **WHEN** a single source IP accumulates more than `RATELIMIT_AUTH_FAILURES_PER_MIN` rejected requests within a minute
- **THEN** the system SHALL respond `429 Too Many Requests` to further requests from that IP for the remainder of the window
