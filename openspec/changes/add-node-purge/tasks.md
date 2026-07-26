## 1. Domain

- [ ] 1.1 Add `NODE_PURGED = "node.purged"` to `AuditAction` in `src/cyberfs/domain/audit.py`
- [ ] 1.2 Confirm it lands in `SECURITY_ACTIONS` and *not* in `ACTIVITY_ACTIONS`, so it is retained rather than pruned
- [ ] 1.3 Confirm it feeds no `SUMMARY_BUCKETS` counter (a purge is not a deletion for activity-summary purposes -- the soft delete already counted)

## 2. Shared purge sequence

- [ ] 2.1 Factor the per-node destructive sequence out of `PurgeJob` (`application/jobs.py`) into a helper both the job and the new use case call
- [ ] 2.2 Keep the quota release charged to `node.owner_id`, never the caller
- [ ] 2.3 Have the helper return the bytes reclaimed and the object count, so both callers can report and log them
- [ ] 2.4 Verify `PurgeJob` behaviour is unchanged after the refactor -- its existing tests must pass untouched

## 3. Use case

- [ ] 3.1 Add a `purge_node` use case in `src/cyberfs/application/nodes.py`
- [ ] 3.2 Refuse a node that is not soft-deleted, with a domain error the API maps to `409`
- [ ] 3.3 Refuse a caller who is neither owner nor administrator; a share recipient must not qualify
- [ ] 3.4 Walk the trashed subtree and delete every descendant's objects and version rows explicitly, before removing the root row
- [ ] 3.5 Sum the reclaimed bytes across the subtree and release the owner's quota once
- [ ] 3.6 Record a `NODE_PURGED` audit entry naming the actor and the owner separately
- [ ] 3.7 Invalidate cached permissions and listings for the affected subjects
- [ ] 3.8 Check whether an in-flight async rewrap for the subtree is affected (design.md, Open Questions) and record the answer

## 4. API

- [ ] 4.1 Add `POST /api/v1/nodes/{node_id}/purge`
- [ ] 4.2 Return `404` for an unknown or already-purged node, never a success
- [ ] 4.3 Return the reclaimed byte count in the response so a caller can show what was freed
- [ ] 4.4 Confirm the route appears in the OpenAPI schema with its error responses

## 5. Tests

- [ ] 5.1 Unit: purging a trashed file deletes its objects, versions, keys, grants, and links
- [ ] 5.2 Unit: purging a live node raises the conflict error and destroys nothing
- [ ] 5.3 Unit: purging a trashed folder deletes every descendant's objects (the stranded-object regression)
- [ ] 5.4 Unit: the owner's quota drops by the subtree's bytes, and an admin purge charges the owner not the admin
- [ ] 5.5 Unit: a share recipient is refused
- [ ] 5.6 Unit: `NODE_PURGED` is a security action and survives an activity prune
- [ ] 5.7 Integration: the endpoint returns `409` for a live node and `404` for an unknown one
- [ ] 5.8 Integration: quota reported by the API drops immediately after a purge
- [ ] 5.9 Integration: a purged node's public link stops resolving
- [ ] 5.10 E2E: purge a trashed folder against a deployment and confirm it leaves the root clean
- [ ] 5.11 Switch the e2e scratch teardown from delete to purge, so the suite stops leaving trash behind

## 6. Verification and documentation

- [ ] 6.1 `just lint`, `just typecheck`, and `just test-unit` clean
- [ ] 6.2 `just test-integration` clean against real Postgres, Redis, and MinIO
- [ ] 6.3 `just test-e2e` clean against the deployment
- [ ] 6.4 Document the endpoint and its irreversibility in the API docs, and cross-reference `docs/restore-runbook.md` for what a prior backup does and does not guarantee
- [ ] 6.5 Run `openspec validate add-node-purge`
