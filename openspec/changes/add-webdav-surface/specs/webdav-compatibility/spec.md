## ADDED Requirements

### Requirement: WebDAV protocol surface

CyberFS SHALL expose a WebDAV Class 1 surface at a configurable base path, supporting `OPTIONS`, `PROPFIND`, `GET`, `HEAD`, `PUT`, `DELETE`, `MKCOL`, `COPY` and `MOVE`. The surface SHALL be available by default and SHALL be switchable off.

#### Scenario: The surface is available without being asked for

- **WHEN** a deployment sets no WebDAV configuration
- **THEN** the surface SHALL be mounted, so a file manager can reach a deployment nobody configured for it

#### Scenario: The surface can be switched off

- **WHEN** WebDAV is explicitly disabled
- **THEN** no WebDAV route SHALL exist, and a request to the base path SHALL be indistinguishable from a request to any other unmapped path

#### Scenario: Plaintext is refused rather than served

- **WHEN** a request reaches the surface over plaintext in a production deployment
- **THEN** the system SHALL refuse it, because Basic authentication carries the secret on every request and the surface is mounted by default -- a deployment that never opted in must not leak a credential per request

#### Scenario: Compliance is advertised honestly

- **WHEN** a client sends `OPTIONS` to the base path
- **THEN** the response SHALL advertise `DAV: 1` and SHALL list exactly the methods the surface implements, so a client is never invited to use one that is absent

#### Scenario: Locking is refused rather than faked

- **WHEN** a client sends `LOCK` or `UNLOCK`
- **THEN** the system SHALL refuse the method, because CyberFS has no lock concept and a lock that does not lock is worse than none

#### Scenario: Failures are WebDAV failures

- **WHEN** any WebDAV request fails
- **THEN** the response SHALL carry a WebDAV-appropriate status and SHALL NOT return the JSON problem document the REST surface uses

### Requirement: WebDAV authentication

The WebDAV surface SHALL authenticate with HTTP Basic credentials that are an existing S3 access key id and secret, and SHALL NOT accept any other credential.

#### Scenario: An access key authenticates

- **WHEN** a client presents an active access key id as the Basic username and its secret as the password
- **THEN** the request SHALL be authorized as that key's subject, with the same effective permissions that subject has over REST

#### Scenario: A missing credential is challenged

- **WHEN** a request arrives with no `Authorization` header
- **THEN** the system SHALL respond `401` with a `WWW-Authenticate: Basic` challenge, so a client knows what to offer

#### Scenario: A wrong or revoked credential is refused

- **WHEN** the key is unknown, the secret is wrong, or the key has been revoked
- **THEN** the system SHALL refuse the request, and SHALL NOT reveal which of those was the case

#### Scenario: Verification does not leak by timing

- **WHEN** an unknown key id is presented
- **THEN** the system SHALL perform the same unseal and comparison work as it would for a real key, so the response time does not distinguish them

#### Scenario: A bearer token is not accepted here

- **WHEN** a client presents an OAuth bearer token to the WebDAV surface
- **THEN** the system SHALL refuse it, because a surface that accepts two credential kinds has two authentication paths to audit and only one that clients actually use

### Requirement: WebDAV namespace mapping

A caller's WebDAV namespace SHALL be their own tree, with paths mapping to nodes by name.

#### Scenario: The base path is the caller's root

- **WHEN** a client lists the base path
- **THEN** the contents SHALL be the children of that caller's root folder

#### Scenario: A path resolves to a node

- **WHEN** a client addresses a path of names beneath the base path
- **THEN** the system SHALL resolve it to the node at that path in the caller's tree, and SHALL respond `404` when no such node exists

#### Scenario: A trashed node is absent

- **WHEN** a node has been soft-deleted
- **THEN** it SHALL NOT appear in any WebDAV listing and SHALL NOT be addressable

#### Scenario: Another caller's tree is unreachable

- **WHEN** a client addresses a path that would resolve into a tree they do not own
- **THEN** the system SHALL refuse, on the same terms as the REST surface

### Requirement: WebDAV property reporting

`PROPFIND` SHALL report the properties a file manager needs, derived from node metadata and never from content.

#### Scenario: A collection and a file are distinguishable

- **WHEN** a client issues `PROPFIND` on a folder with `Depth: 1`
- **THEN** the response SHALL be a multistatus document in which the folder is a collection and each file is not, so a client can render the tree

#### Scenario: Properties come from metadata

- **WHEN** properties are reported for a file
- **THEN** they SHALL include its display name, content length, content type, last-modified time and entity tag, all taken from stored metadata

#### Scenario: The entity tag matches the REST one

- **WHEN** the same node is read over WebDAV and over REST
- **THEN** the entity tag SHALL be identical, so a client that caches on one surface is not misled by the other

#### Scenario: Depth is bounded

- **WHEN** a client issues `PROPFIND` with a depth beyond what the surface supports
- **THEN** the system SHALL refuse rather than walking an unbounded subtree

### Requirement: WebDAV operations reuse the existing model

Every WebDAV mutation SHALL go through the same use cases as its REST equivalent, so no rule is enforced on one surface and not the other.

#### Scenario: An upload is charged and encrypted like any other

- **WHEN** a client `PUT`s a file into a folder whose encryption default is on
- **THEN** the content SHALL be encrypted, the owner's quota SHALL be charged, and the operation SHALL appear in that caller's activity, exactly as an equivalent REST upload would

#### Scenario: A delete is recoverable

- **WHEN** a client sends `DELETE`
- **THEN** the node SHALL be soft-deleted rather than destroyed, so WebDAV cannot be used to bypass the trash

#### Scenario: An upload beyond quota is refused

- **WHEN** a `PUT` would exceed the owner's quota
- **THEN** the system SHALL refuse it and SHALL store neither content nor metadata

#### Scenario: MOVE and COPY honour the destination header

- **WHEN** a client sends `MOVE` or `COPY` with a `Destination`
- **THEN** the system SHALL apply it as the equivalent rename, move or copy, and SHALL refuse when the destination already exists and overwriting was not requested

#### Scenario: A name WebDAV allows but CyberFS does not is refused

- **WHEN** a client creates a resource whose name CyberFS rejects
- **THEN** the system SHALL refuse it with a WebDAV status rather than storing a name the REST surface could not represent
