# Activity in CyberFS

CyberFS records every operation a user performs on a node and exposes each user
their own history — a rollup of counts and byte totals plus a paginated feed of
the individual operations. The same records that power this feed are the audit
trail; activity is simply the subset of the audit log that describes *what a
user did*, as opposed to the security events kept as evidence.

This document describes what the code actually does. The requirements it
implements live in
`openspec/changes/add-s3-and-activity/specs/activity-reporting/spec.md`.

## What is recorded

Every file operation writes one immutable `AuditRecord`
(`domain/audit.py`). The operations that count as *activity* are fixed in
`ACTIVITY_ACTIONS` (`domain/activity.py`):

| Action | When |
|---|---|
| `file.uploaded` | a new file or version is stored |
| `file.downloaded` | content is read |
| `node.created` | a folder or empty node is created |
| `node.renamed` | a node is renamed |
| `node.moved` | a node is moved |
| `node.copied` | a node is copied |
| `node.deleted` | a node is trashed |
| `node.restored` | a node is restored from trash |
| `version.restored` | an older version is restored |
| `public_link.accessed` | content is fetched through a public link |

Each record carries the acting subject, the target node id, the operation, the
protocol it arrived through, the time, and — for uploads and downloads — the
**plaintext** byte count in `context["bytes"]`. Byte totals reported by the
summary are those plaintext sizes, matching what the user actually sent or
received (`application/content.py`, `_audit_download` and the upload path;
`bytes_downloaded_total` counts served bytes independently).

A record **never** holds file content, and it never holds a file name unless the
actor owns the node. That last rule is enforced once, in
`owner_context(node, actor_id)` (`application/auditing.py`): it returns
`{"name": …}` only when `node.owner_id == actor_id`, and `{}` otherwise. Writing
the record can never fail the operation it describes — `emit_audit` swallows and
logs any persistence error at `error` level, because losing a log line must not
lose a user's file.

### Reads of another user's file, and public links

- A **share recipient** who downloads an owner's file is recorded as the actor,
  against the owner's node id; the owner's file name is omitted because the
  recipient does not own it. This download counts toward the *recipient's* own
  activity, not the owner's.
- A **public-link** read has no authenticated caller, so it is attributed to the
  link (`context["link_id"]`) with `actor_subject = None` and never names the
  owner (`application/content.py`, `_audit_download`).

### Denials are not activity

A refused operation is recorded as an authorization denial (`auth.denied`) via
`authorize_or_record` / `emit_denial` (`application/auditing.py`), carrying the
actor and node id only. It never appears in the actor's activity as though it
succeeded — `auth.denied` is a security action, not an activity action.

## The protocol field

Every record carries an `AuditProtocol` (`domain/audit.py`): `rest` or `s3`. It
records the surface an operation arrived through so REST and S3 traffic can be
told apart in the same feed. It defaults to `rest`; S3 handlers record `s3`. The
value is surfaced on each feed item as `protocol` (`schemas.ActivityItem`).

## The endpoint

`GET /api/v1/me/activity` (`adapters/inbound/api/routers/me.py`).

Query parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `window_days` | `30` | how far back to look, `≥ 1` |
| `action` | — | narrow the feed to one action (the summary stays unfiltered) |
| `limit` | `50` | page size, `1`–`1000` |
| `cursor` | — | opaque cursor for the next page |

The response (`schemas.ActivityResponse`) is a `summary` plus a newest-first
`items` list and a `next_cursor`:

- **summary** (`ActivitySummary`) — `window_start`/`window_end`, counts of
  `uploads`, `downloads`, `shares_granted`, `shares_revoked`, `deletions`,
  `restores`, `bytes_uploaded`, `bytes_downloaded`, and `busiest_day`. The
  rollup is a pure fold over grouped `(action, day)` counts
  (`domain/activity.build_rollup`); the busiest day breaks ties toward the later
  date so the answer is deterministic. Note that `shares_granted` /
  `shares_revoked` count grant changes even though grants are *security*
  records — the summary describes what the user did, not what is retained
  (`SUMMARY_BUCKETS`).
- **items** (`ActivityItem`) — each carries `action`, `occurred_at`, `node_id`,
  `node_name`, and `protocol`.

A window longer than `ACTIVITY_MAX_WINDOW_DAYS` is refused with `422` rather than
scanning an unbounded range (`application/activity.ActivityService.activity`).

## The privacy boundary

Four rules, each enforced in code rather than by convention:

1. **Self-scoped by construction.** Every read passes exactly `user.subject`;
   there is no path or query parameter naming another subject
   (`application/activity.py`, `routers/me.py`). A caller who supplies some other
   identifier is simply ignored and gets their own activity back.
2. **Service principals are refused.** A service principal owns no tree, so it is
   turned away with `403` at the endpoint, before a `User` is ever resolved
   (`routers/me.py`, `principal.is_service`).
3. **Non-owned nodes carry no name.** The feed resolves names only for nodes the
   caller owns; a node owned by someone else is returned by `node_id` with
   `node_name = null` (`adapters/outbound/db/activity_queries.py`,
   `_owned_names` joins `NodeRow.owner_id == actor_id`).
4. **Purged nodes are kept by id.** An entry whose node has since been purged is
   still returned — history must not vanish when its subject does — again by id
   only, since the name lookup finds nothing (`activity_queries.py`,
   `_owned_names` / `_to_entry`).

An owner does not see a recipient's read here, and an administrator does not use
this endpoint for other users' activity: both go through the admin audit surface,
which is separately access-controlled and itself audited.

## Retention

Two clocks, on purpose (`application/jobs.py`, `ActivityPruneJob`;
`domain/activity.py`):

- **Activity records** are kept for `ACTIVITY_RETENTION_DAYS`
  (`activity_retention_days`, default **90 days**;
  `infrastructure/settings.py`). The `activity_prune` job deletes records older
  than the cutoff, passing only the `ACTIVITY_ACTIONS` set to the delete
  (`uow.audit.prune_activity(cutoff, actions=…)`).
- **Security records** — authorization denials, grant changes, ownership
  transfers, encryption changes, S3 access-key lifecycle, and administrative
  actions (everything in `SECURITY_ACTIONS`, derived as *not* an activity
  action) — are never touched by this job and are retained under the longer
  audit retention. They are evidence, not activity: losing an activity line is a
  shrug; losing an evidence line is not.

`SECURITY_ACTIONS` is derived by subtraction from `ACTIVITY_ACTIONS`, so a newly
added action is retained by default — forgetting to classify a new event keeps
it, which is the safe direction.

`ACTIVITY_MAX_WINDOW_DAYS` (`activity_max_window_days`, default **90 days**) is a
separate bound: it is the longest window the endpoint will *answer*, unrelated to
how long records are *kept*.
