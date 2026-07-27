## Context

CyberFS has two surfaces. REST is the primary one; S3 was added because "S3 is
what tooling speaks" — and for object tooling it is. For a *file manager*, it is
not: nothing in Finder, Explorer or a Linux desktop mounts an S3 bucket without
extra software, while all of them mount WebDAV natively.

`routers/s3.py` is the template. One router factory rooted at a configurable base
path, mounted only when its setting is on, every failure rendered in the
protocol's own error format rather than the REST problem document, and every
mutation delegated to the same use case the REST router calls.

## Goals / Non-Goals

**Goals:**

- A WebDAV Class 1 surface that `rclone`, `davfs2` and desktop file managers can
  mount and use for real work.
- FUSE, delivered by `rclone mount` over that surface rather than by a driver.
- One credential across protocols: the access keys that already exist.
- No rule enforceable on REST but not on WebDAV.

**Non-Goals:**

- `LOCK`/`UNLOCK`, and therefore Class 2. See below.
- `PROPPATCH` and dead properties.
- A FUSE driver of our own.
- WebDAV extensions: `REPORT`, versioning (RFC 3253), ACLs (RFC 3744), quota
  properties (RFC 4331). Quota reporting is tempting and cheap, but it is a
  property vocabulary decision that belongs with `PROPPATCH`.
- Serving WebDAV over plaintext in production.

## Decisions

**Reuse the S3 access keys instead of adding a WebDAV credential.** They are
already exactly this: a long-lived secret, sealed under `MASTER_KEY`, revocable in
one write, audited on creation and revocation, and specified as "a credential, not
an identity". The alternative — a WebDAV-specific credential — would duplicate a
lifecycle, a revocation path and an audit trail for no gain, and would force a
user who wants both surfaces to manage two secrets. The requirement is amended to
say keys serve every surface, so this stops being an S3 detail.

**Basic, not Bearer.** WebDAV clients overwhelmingly speak Basic; several cannot
send an arbitrary `Authorization` header at all. Accepting both would mean two
authentication paths to audit, one of which nothing uses. The cost of Basic is
that the secret travels on every request, which is why the surface must refuse
plaintext in production.

**Constant-time verification, copied deliberately from `s3_auth.py`.** An unknown
key id must cost the same as a real one, or the response time distinguishes
"no such key" from "wrong secret" and turns key ids into an enumerable space.
`s3_auth.py` already solves this with a placeholder sealed secret it unseals when
the lookup misses; the WebDAV verifier uses the same device rather than a fresh
one.

**Class 1 only, and say so.** Class 2 requires `LOCK`/`UNLOCK`. CyberFS has no
lock concept, and the concurrency control it does have — `If-Match` against a node
revision — is optimistic, whereas WebDAV locks are pessimistic. Bolting on a lock
table would put a second, weaker concurrency model beside the working one, and a
lock that does not actually prevent a concurrent REST write is a lie told to the
client. Advertising `DAV: 1` is honest; Windows Explorer and Finder will mount
read-only or refuse writes as a result, and that is the documented cost.
`rclone` and `davfs2` are unaffected.

**Every method delegates to an existing use case.** `PUT` goes through
`ContentService.upload`, `DELETE` through `NodeService.delete`, `MKCOL` through
`create_folder`, `MOVE` through `rename`/`move`, `COPY` through `copy`. This is
the property that matters most: quota, encryption inheritance, the trash,
auditing and the activity feed cannot drift between surfaces, because there is
only one implementation of each. A WebDAV layer that reimplemented any of them
would be a second place for a rule to be wrong.

**Paths resolve by walking names, and the walk is bounded.** WebDAV addresses
nodes by path while CyberFS stores an adjacency list, so each segment is a
`get_child_by_name` against the previous node. That is one query per segment; the
existing depth limit bounds it, and a path longer than the limit is refused
rather than walked.

**`PROPFIND` supports Depth 0 and 1 only.** `Depth: infinity` is a recursive walk
of an unbounded subtree in one request, which is how WebDAV servers are made to
denial-of-service themselves. Refusing it is permitted by RFC 4918 and is what
most servers do.

**The entity tag is the REST ETag, unchanged.** A client that reads a node over
both surfaces must not see two different tags for one state.

## Risks / Trade-offs

- **Basic authentication sends the secret on every request.** Over TLS that is
  acceptable and universal; over plaintext it is a credential leak per request.
  → Mitigation: the surface refuses to serve in production unless the request
  arrived over TLS, and the documentation says why. Keys are revocable in one
  write, so a leak is contained the way an S3 key leak is.

- **No locking means desktop clients degrade.** Explorer and Finder may mount
  read-only.
  → Mitigation: documented plainly, with `rclone` recommended for read-write use.
  Faking locks would be worse than the limitation.

- **A new surface is a new place for an authorization mistake.** Three surfaces
  now reach the same data.
  → Mitigation: every method delegates, so authorization happens in the use case
  that already enforces it. The tests assert the negative cases directly — another
  caller's tree, a trashed node, an over-quota upload — rather than trusting that
  delegation implies them.

- **Path-walking costs a query per segment.** Deep trees make listing chatty.
  → Mitigation: bounded by the existing depth limit; measured rather than assumed
  if it becomes a complaint. A materialized path column is the answer if so, and
  it is a change of its own.

## Migration Plan

Purely additive and off by default: no migration, no new table, no new credential,
and no route exists until `WEBDAV_ENABLED` is set. Rolling back is unsetting it.
Nothing else in the system can tell whether the surface is mounted.

## Open Questions

- Should `WEBDAV_ENABLED` default on in the deployment once verified? It is off in
  this change because a surface that authenticates with Basic should be switched on
  deliberately, not inherited.
- RFC 4331 quota properties would let a file manager show free space, which is a
  real usability gain. Deferred with `PROPPATCH` because both are property
  vocabulary decisions, but it is the more valuable of the two.
