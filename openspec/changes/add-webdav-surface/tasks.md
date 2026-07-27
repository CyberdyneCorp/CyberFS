## 1. Configuration

- [x] 1.1 Add `WEBDAV_ENABLED` (default **true**) and `WEBDAV_BASE_PATH` (default `/webdav`) to `Settings`
- [x] 1.2 Document both in `.env.example` and `coolify.yaml`, stating that the surface is on by default, that Basic auth carries the secret on every request, and how to switch it off
- [x] 1.3 Refuse to serve in production unless the request arrived over TLS, and unit-test the
      refusal. This is the load-bearing guard now that the surface is mounted by default: it is
      what stops a deployment that never opted in from leaking a credential per request
- [x] 1.4 Confirm nothing is disclosed before authentication -- no method may reveal whether a
      path exists to an unauthenticated caller, since the surface is now present everywhere

## 2. Domain

- [x] 2.1 Add `src/cyberfs/domain/webdav/` with multistatus XML generation, pure and HTTP-free
- [x] 2.2 Map node metadata to the DAV properties: displayname, getcontentlength, getcontenttype, getlastmodified, getetag, resourcetype
- [x] 2.3 Use the node's existing ETag verbatim, so WebDAV and REST never disagree
- [x] 2.4 Escape names correctly in XML and percent-encode hrefs, so a name with `&` or a space cannot break the document

## 3. Authentication

- [x] 3.1 Add `application/webdav_auth.py` verifying Basic credentials against `S3AccessKeyService`
- [x] 3.2 Use the same placeholder-unseal device as `s3_auth.py` so an unknown key id costs what a real one does
- [x] 3.3 Return `401` with `WWW-Authenticate: Basic` when no credential is offered
- [x] 3.4 Refuse a bearer token on this surface

## 4. Path resolution

- [x] 4.1 Resolve a WebDAV path to a node by walking `get_child_by_name` per segment from the caller's root
- [x] 4.2 Bound the walk by the existing depth limit and refuse anything longer
- [x] 4.3 Treat a trashed node as absent
- [x] 4.4 Percent-decode segments, and refuse a segment CyberFS would reject as a name

## 5. Methods

- [x] 5.1 `OPTIONS`: advertise `DAV: 1` and exactly the implemented methods
- [x] 5.2 `PROPFIND` Depth 0 and 1, returning `207` multistatus; refuse `Depth: infinity`
- [x] 5.3 `GET`/`HEAD` delegating to the content service, including `Range`
- [x] 5.4 `PUT` delegating to `ContentService.upload`
- [x] 5.5 `DELETE` delegating to `NodeService.delete` (soft delete, not destruction)
- [x] 5.6 `MKCOL` delegating to `create_folder`
- [x] 5.7 `MOVE` and `COPY` honouring `Destination` and `Overwrite`
- [x] 5.8 `LOCK`/`UNLOCK` refused with `405`
- [x] 5.9 Render every failure as a WebDAV status, never the REST problem document

## 6. Wiring

- [x] 6.1 Mount the router in `create_app` only when enabled, mirroring the S3 gate
- [x] 6.2 Confirm the routes are absent from the OpenAPI schema when disabled
- [x] 6.3 Label the routes for the metrics middleware so WebDAV traffic is distinguishable

## 7. Unit tests

- [x] 7.1 Multistatus XML: a collection and a file are distinguishable; names needing escaping survive
- [x] 7.2 ETag equals the REST ETag for the same node
- [x] 7.3 Basic auth accepts an active key, refuses unknown, wrong secret and revoked
- [x] 7.4 A missing credential yields `401` with the challenge
- [x] 7.5 A bearer token is refused
- [x] 7.6 Path resolution: nested path resolves, unknown path is `404`, trashed node is absent
- [x] 7.7 A path deeper than the limit is refused
- [x] 7.8 `Depth: infinity` is refused
- [x] 7.9 `LOCK` and `UNLOCK` return `405`
- [x] 7.10 Plaintext in production is refused
- [x] 7.11 No route exists when explicitly disabled
- [x] 7.12 The surface IS mounted with no WebDAV configuration set, pinning the new default so a
      later change cannot flip it back silently

## 8. Integration tests

- [x] 8.1 `PROPFIND` on the root lists the caller's children
- [x] 8.2 `PUT` then `GET` round-trips bytes identically
- [x] 8.3 `PUT` into a folder with encryption on stores ciphertext and reads back plaintext
- [x] 8.4 `PUT` beyond quota is refused and stores nothing
- [x] 8.5 `DELETE` soft-deletes: the node is gone from listings and restorable over REST
- [x] 8.6 `MKCOL` creates a folder visible over REST
- [x] 8.7 `MOVE` renames, `COPY` duplicates, and an unrequested overwrite is refused
- [x] 8.8 A file written over WebDAV is visible over REST with the same digest, and the reverse
- [x] 8.9 Another caller's path is unreachable
- [x] 8.10 A revoked key stops working immediately

## 9. Client verification, on the real server

- [ ] 9.1 Enable the surface on the deployment and confirm `OPTIONS` advertises `DAV: 1`
- [ ] 9.2 `rclone lsd`/`ls`/`copy`/`cat` against the live surface with a real access key
- [ ] 9.3 `rclone mount` — the FUSE half — and confirm a file written through the mount is readable over REST
- [ ] 9.4 Record what each client can and cannot do, including the Class 2 limitation

## 10. Documentation

- [x] 10.1 `docs/webdav.md`: mounting with `rclone`, `davfs2`, Finder and Explorer
- [x] 10.2 State plainly that FUSE is `rclone mount` over this surface, not a driver we ship
- [x] 10.3 State the Class 2 limitation and which clients it affects
- [x] 10.4 Record the non-goal reversal in `README.md`'s surface list

## 11. Verification

- [x] 11.1 `just lint`, `just typecheck`, `just test-unit` clean
- [ ] 11.2 `just test-integration` green in CI, verified rather than assumed
- [ ] 11.3 `just test-e2e` green against the deployment
- [x] 11.4 `openspec validate add-webdav-surface --strict`
