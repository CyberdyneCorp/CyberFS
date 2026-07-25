## Context

CyberFS has a complete REST backend: tree, content, sharing, optional
encryption, caching, and an admin surface. Two things it does not have are an
interface anything else already speaks, and any way for a user to see what they
did.

This change adds both. They arrive together because they share one prerequisite:
neither works until ordinary file operations are recorded, and today they are
not. The audit log covers grants, authentication failures, encryption changes,
and administrative actions — uploads, downloads, creates, moves, and deletions
leave no trace at all.

**This reverses a documented decision.** `design.md` in `bootstrap-cyberfs`
lists "WebDAV, FUSE, S3-compatible gateway, or any other protocol surface beyond
REST" as a non-goal. That was the right call for a v1 whose job was to establish
a permission and encryption model. It is the wrong call now: the model exists
and is tested, and the cost of *not* having S3 is paid by every product team
that has to write a client. The reversal applies to S3 only; WebDAV and FUSE
remain out of scope, because neither is what tooling actually speaks.

Decisions fixed before design began:

- S3 requests authenticate by **SigV4 against CyberFS-issued keys, or by
  CyberdyneAuth bearer token** — both resolving to the same subject.
- **One bucket per user**, named for their subject, with shared items under a
  reserved `shared/<owner>/…` prefix.
- Activity returns **rollup counts and a paginated feed**, not one or the other.

## Goals / Non-Goals

**Goals:**

- Standard S3 tooling works unmodified against CyberFS, with the same
  permissions, quota, versioning, and encryption as REST.
- Exactly one implementation of every rule. The S3 surface is a protocol
  adapter over the existing use cases, not a second filesystem.
- A user can answer "what did I do, and what happened to my files" without
  asking an administrator.
- Recording activity never endangers the operation being recorded.

**Non-Goals:**

- WebDAV and FUSE. Still out of scope; S3 is what tooling speaks.
- S3 object versioning, lifecycle rules, bucket policies, ACLs, replication,
  storage classes, tagging, or object lock. CyberFS has its own versioning and
  permission model, and mapping S3's onto it would create two ways to express
  the same thing that could disagree.
- Client-managed buckets. Buckets correspond to users.
- Anonymous S3 access. Public sharing already exists as public links.
- Activity for anyone but the caller. Cross-user visibility is the audit log's
  job, and it is already access controlled.
- Real-time activity streaming. A polled endpoint is enough for the question
  being asked.

## Decisions

### Both SigV4 and bearer, on one surface

**Decision.** The S3 endpoint accepts either an `AWS4-HMAC-SHA256` signature over
a CyberFS-issued access key, or an `Authorization: Bearer` CyberdyneAuth token.
Presenting both is a `400`. Both resolve to a `Principal` and rejoin the existing
authorization path immediately.

**Why.** SigV4 is not optional if the goal is that `aws-cli` and `rclone` work —
they sign every request and cannot be told not to. But requiring first-party
Cyberdyne services to mint and store long-lived S3 keys, when they already hold a
token, would spread exactly the credential we least want spread. Supporting both
costs one branch at the very edge of the request.

**Alternatives considered.** *SigV4 only* — cleanest surface, but pushes every
internal caller into holding a static secret. *Bearer only* — trivial, and
delivers S3-shaped URLs that no S3 client can use, which is not compatibility.

**The risk taken seriously.** Two auth paths over one surface is two sets of edge
cases. The mitigation is that both produce the same `Principal` type within a few
lines of entry, so everything downstream — permission resolution, quota,
encryption — has a single path. The specs require a test that the same operation
by the same subject over both credentials reaches an identical decision.

### An access key is a credential, never an identity

**Decision.** Keys resolve to an existing CyberdyneAuth subject and confer
nothing on their own. They cannot carry admin rights: a key-authenticated caller
is never treated as an administrator, and admin routes reject key authentication
outright.

**Why.** A long-lived static secret is the credential most likely to end up in a
CI log, a `.env` committed by accident, or a laptop backup. `authentication/spec.md`
already requires administrative actions to be introspection-backed precisely
because staleness is dangerous there; a credential that never expires is the
extreme case of stale. Denying admin over keys costs an administrator nothing —
they still have a browser and a token — and removes the worst outcome of a leak.

**Consequence accepted.** Revocation-sensitive operations (grants, transfers)
*are* permitted over keys, but still introspect the owning subject first, and
still fail closed if CyberdyneAuth is unreachable.

### One bucket per user, shares under a reserved prefix

**Decision.** `ListBuckets` returns exactly one bucket, named for the caller's
subject. Their tree maps to keys. Nodes shared with them appear under
`shared/<owner-subject>/…`. `CreateBucket` and `DeleteBucket` are refused, and a
real folder named `shared` cannot be created at the root.

**Why.** S3 has two levels of naming and CyberFS has one tree, so something has
to give. A bucket per user gives each caller a namespace that matches what they
already see, and keeps another user's bucket name from being a probe for whether
that user exists — addressing someone else's bucket is `NoSuchBucket`, the same
answer as a bucket that was never there.

The reserved prefix is the part that needed care. Without it a recipient would
need a separate credential or a separate bucket per sharer, and neither survives
contact with a sync client. With it, everything the caller may reach is under one
bucket. The cost is that `shared` becomes a reserved name at the root of every
tree, which the spec makes explicit rather than letting a collision surprise
someone.

**Alternatives considered.** *Bucket per top-level folder* — maps nicely onto
tools that show buckets as drives, but then `CreateBucket` creates a folder and
`DeleteBucket` destroys a subtree, and shared items have nowhere to live.
*One global bucket with owner prefixes* — closest to the physical layout, but
every user faces one enormous namespace and prefix matching becomes the only
thing standing between users.

### The S3 surface is an adapter, not a second filesystem

**Decision.** Every S3 operation calls the same application use case as its REST
equivalent. The adapter's job is parsing, signing, XML, and the key-to-node
mapping — nothing else.

**Why.** The danger with a second protocol is not that it is wrong on day one but
that the two paths drift, and the drift lands on permission or encryption checks
rather than somewhere harmless. Sharing the use cases makes "S3 enforces the same
rules" true by construction rather than by a test suite that must be remembered.

**Consequence.** Some S3 semantics do not map cleanly and are refused rather than
approximated: object versioning parameters are ignored (CyberFS versioning is not
S3 versioning), and `DeleteObject` moves to trash rather than destroying, because
that is what CyberFS deletion means.

### Presigned URLs point at CyberFS, and the existing rule is sharpened

**Decision.** CyberFS may issue presigned URLs; every one addresses CyberFS's own
S3 endpoint. `file-storage/spec.md`'s prohibition is restated as: no URL or
credential granting direct access to the *underlying object store*.

**Why.** The original rule exists so that every byte is subject to authorization,
quota, and decryption. A presigned URL CyberFS itself honours preserves all
three; a presigned MinIO URL destroys all three. As written, the old rule read as
though it banned both, which would have made S3 compatibility impossible for the
wrong reason. Sharpening it is a clarification of intent, not a weakening — and
the spec adds a scenario asserting the MinIO endpoint appears in no response from
any surface.

### Activity is built on audit records, and file operations become auditable

**Decision.** Uploads, downloads, creates, moves, deletions, and restores emit
audit records. The activity endpoint reads them, returning both a rollup and a
paginated feed. Records carry a protocol marker so REST and S3 traffic are
distinguishable.

**Why.** The audit table already exists, is already immutable, and is already
indexed by actor and time. Building a second history would mean two sources of
truth about the same events. Recording file operations also closes a real gap:
an administrator investigating an incident currently cannot see that a file was
read at all.

**The cost accepted.** Recording every download turns the quietest table in the
schema into the busiest. Hence a separate, shorter retention for activity records
than for security records, and an explicit requirement that the summary be
answerable from an index rather than a scan.

**The rule that matters.** A failure to write an activity record must never fail
the operation. Losing a log line is a reporting gap; losing a user's upload
because logging was down is data loss.

### Activity is private to its subject

**Decision.** The endpoint returns only the caller's own operations, and takes no
parameter naming another user. Administrators use the audit log.

**Why.** Two surfaces onto the same data with different access rules is how
authorization bugs happen. Keeping the self-service endpoint incapable of
expressing "someone else" means there is no parameter to get wrong, and the
admin path stays the one that is already access controlled and already audited.

## Risks / Trade-offs

- **Signature verification is security-critical and easy to get subtly wrong** →
  Implemented against the published SigV4 algorithm with canonical-request tests
  drawn from AWS's own documented examples; constant-time comparison; body hash
  checked so a signature cannot be replayed over altered content; skew bounded.
- **Long-lived access keys leak** → Never admin-capable; revocable with immediate
  effect; last-used recorded so unused keys can be found and retired; multiple
  keys supported so rotation needs no outage; secret stored only as a verifier.
- **Two auth paths drift** → Both produce the same `Principal` within a few lines
  of the edge; a spec scenario requires identical decisions across both.
- **The S3 adapter drifts from REST** → It calls the same use cases. Adding a
  rule in the application layer applies to both surfaces automatically.
- **The `shared/` prefix collides with a real folder** → Reserved at the root of
  every tree and refused at creation, rather than silently shadowed.
- **Bucket names reveal who exists** → Another subject's bucket answers
  `NoSuchBucket`, identical to a name that was never used.
- **Audit volume grows sharply** → Shorter retention for activity than for
  security records, a prune job, and an index that the summary query must be
  answerable from.
- **Activity recording becomes a write-path dependency** → Explicitly
  non-blocking: a failed record is logged and the operation proceeds.
- **Multipart uploads leave debris** → Abandoned uploads are reclaimed by the
  existing orphan reaper after a grace period, on the same basis as interrupted
  single-part uploads.
- **Scope creep into full S3** → Lifecycle, ACLs, bucket policies, replication,
  object lock and tagging are explicit non-goals; unsupported operations return
  `NotImplemented` rather than a partial imitation.

## Migration Plan

Both capabilities are additive; nothing existing changes behaviour.

1. **Operation auditing** — record file operations and add the protocol marker.
   This is the shared prerequisite and lands first, on its own.
2. **Activity endpoint** — rollup, feed, retention, prune job. Deliverable and
   useful immediately, and it exercises the new records before anything depends
   on them more heavily.
3. **Access keys** — creation, listing, revocation, storage, last-used tracking.
   No S3 surface yet; keys are inert until there is something to sign.
4. **SigV4 verification** — canonical request, signature, skew, body hash,
   against AWS's documented test vectors.
5. **S3 read path** — `ListBuckets`, `ListObjectsV2`, `HeadObject`, `GetObject`,
   including ranges and the `shared/` prefix. Read-only first, so the mapping is
   proven before anything can write through it.
6. **S3 write path** — `PutObject`, `DeleteObject`, `DeleteObjects`,
   `CopyObject`.
7. **Multipart upload** and abandoned-upload reclamation.
8. **Presigned URLs**, once the surface they point at is complete.

**Rollback.** The S3 surface is mounted behind `S3_API_ENABLED`; turning it off
removes the endpoint without touching stored data, since it introduces no
storage format of its own. Access keys become inert. Activity recording is
independent and would be rolled back only by ceasing to write the new record
types, which no other capability depends on.

## Resolved Questions

Each of these was open when design began; the entry records what shipped and why.

- **Key expiry — resolved: manual revocation only, no optional expiry.**
  `S3AccessKey` carries `created_at`, `last_used_at`, and `revoked_at`, and no
  expiry field (`domain/s3/access_key.py`). `is_active` is simply
  `revoked_at is None`. Revocation takes effect on the very next signed request
  with no cache to expire, multiple keys coexist so rotation needs no outage, and
  last-used is stamped on every authenticated request so an unused key can be
  found and retired. Expiry would have added a renewal burden on exactly the
  unattended clients that are the main use case, with no gain over immediate
  revocation, so it was not built.

- **Anonymous presigned reads — resolved: no; a presigned URL always requires a
  key to sign it.** `S3PresignService.presign` takes an `S3AccessKey` and roots
  the URL at CyberFS's own endpoint (`application/s3_presign.py`); there is no
  keyless form. A URL stops working the instant its signing key is revoked.
  Anonymous S3 access stays a non-goal, and public sharing stays the existing
  public-link mechanism — unifying the two would have widened the unauthenticated
  surface for a convenience the public link already covers.

- **Retention numbers — resolved: 90 days each.** `ACTIVITY_RETENTION_DAYS`
  defaults to `90` and `ACTIVITY_MAX_WINDOW_DAYS` to `90`
  (`infrastructure/settings.py`); both are environment-overridable. Activity
  records are pruned at the retention horizon by the prune job, while security
  records (denials, grants, transfers, encryption changes, admin actions) are
  retained separately and never pruned there. The window cap refuses a query
  asking for more than the maximum with `422` rather than scanning an unbounded
  range.

- **Download-record granularity — resolved: a ranged read counts as a download,
  recording the bytes actually served.** `ContentService.download` records one
  `FILE_DOWNLOADED` audit record per read, carrying `served` — `wanted.length`
  for a range, the full size otherwise — not the object size
  (`application/content.py`). There is no coalescing of a burst of range reads:
  the audit's value is that it reflects what was actually served, and correlating
  many ranges into one logical download would demand exactly the cross-request
  state the immutable audit table avoids. The sharper activity retention and the
  prune job, not deduplication, bound the volume a range-heavy client produces.

- **Bucket naming — resolved: the bucket is exactly the subject, 1:1, no alias.**
  `bucket_for_subject` returns the subject unchanged and `subject_for_bucket` is
  its inverse (`domain/s3/namespace.py`). Subjects are UUIDs, so bucket names are
  unfriendly but unambiguous, invertible, and collision-free, and a foreign
  bucket answers `NoSuchBucket` identically to a name that never existed — so a
  bucket name is never a probe for who exists. An alias would have needed a
  uniqueness authority and reintroduced that enumeration risk, so none was added.
