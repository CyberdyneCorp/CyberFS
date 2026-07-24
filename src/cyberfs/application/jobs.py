"""Background maintenance.

Three sweeps that keep storage and accounting honest: purging expired trash,
reaping objects no row references, and reconciling quota counters against the
rows they summarize.

Each returns a result rather than logging and forgetting, so the admin health
view can report what actually happened.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from cyberfs.application.sharing import KeyRewrapper
from cyberfs.domain.activity import ACTIVITY_ACTIONS
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.nodes import Node
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.ports.storage import ObjectStore, StoredObject
from cyberfs.domain.sharing import Grant
from cyberfs.infrastructure.logging import get_logger
from cyberfs.infrastructure.metrics import job_runs_total, s3_multipart_uploads_in_flight
from cyberfs.infrastructure.settings import Settings

#: Bounds every subtree walk, matching the sharing service's guard.
ANCESTOR_GUARD_DEPTH = 512

logger = get_logger(__name__)


class PermissionInvalidator(Protocol):
    """Drops a subject's cached permission decisions after activation."""

    async def invalidate_permissions_for_subject(self, subject: str) -> None: ...


#: `{owner_id}/{node_id}/{version_id}` -- the only shape a live key can have.
OBJECT_KEY_PATTERN = re.compile(
    r"^(?P<owner>[0-9a-f-]{36})/(?P<node>[0-9a-f-]{36})/(?P<version>[0-9a-f-]{36})$"
)


@dataclass(frozen=True, slots=True)
class PurgeResult:
    nodes_purged: int = 0
    objects_deleted: int = 0
    bytes_reclaimed: int = 0


@dataclass(frozen=True, slots=True)
class ReaperResult:
    objects_scanned: int = 0
    objects_deleted: int = 0
    bytes_reclaimed: int = 0
    #: Multipart uploads abandoned past `S3_MULTIPART_ABANDON_HOURS` and reclaimed.
    uploads_reclaimed: int = 0


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    users_checked: int = 0
    users_corrected: int = 0


@dataclass(frozen=True, slots=True)
class ActivityPruneResult:
    records_pruned: int = 0


@dataclass(frozen=True, slots=True)
class RewrapResult:
    grants_scanned: int = 0
    grants_activated: int = 0
    nodes_rewrapped: int = 0


def parse_object_key(key: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Extract `(node_id, version_id)`, or None if the key is not ours."""
    match = OBJECT_KEY_PATTERN.match(key)
    if match is None:
        return None
    try:
        return uuid.UUID(match["node"]), uuid.UUID(match["version"])
    except ValueError:
        return None


class PurgeJob:
    """Deletes trash that has outlived its retention window.

    The only operation that actually frees space: soft delete merely moves
    bytes between buckets.
    """

    name = "purge"

    def __init__(self, objects: ObjectStore, settings: Settings) -> None:
        self._objects = objects
        self._retention = timedelta(days=settings.trash_retention_days)
        self._batch = settings.page_size_max

    async def run(self, uow: UnitOfWork, *, now: datetime | None = None) -> PurgeResult:
        moment = now or utcnow()
        cutoff = moment - self._retention
        expired = await uow.nodes.list_trashed_before(cutoff, limit=self._batch)

        nodes = objects = reclaimed = 0
        for node in expired:
            reclaimed += await self._purge_node(uow, node.id, node.owner_id, moment)
            objects += await self._purge_objects(uow, node.id)
            # Grants and wrapped keys go with it: a purged node must leave no
            # way for a former recipient to reach anything.
            await uow.grants.delete_for_node(node.id)
            await uow.keys.delete_data_keys_for_node(node.id)
            await uow.nodes.delete_permanently(node.id)
            nodes += 1

        await uow.commit()
        job_runs_total.labels(job=self.name, outcome="success").inc()
        logger.info("purge_completed", nodes=nodes, objects=objects, bytes=reclaimed)
        return PurgeResult(nodes, objects, reclaimed)

    async def _purge_objects(self, uow: UnitOfWork, node_id: uuid.UUID) -> int:
        versions = await uow.versions.list_for_node(node_id)
        for version in versions:
            await self._objects.delete(version.object_key)
            await uow.versions.delete(version.id)
        return len(versions)

    @staticmethod
    async def _purge_node(
        uow: UnitOfWork, node_id: uuid.UUID, owner_id: uuid.UUID, now: datetime
    ) -> int:
        node = await uow.nodes.get(node_id)
        if node is None:
            return 0
        usage = await uow.quotas.get(owner_id)
        if usage is not None:
            usage.purge_from_trash(node.size_bytes, now)
            await uow.quotas.update(usage)
        return node.size_bytes


class OrphanReaper:
    """Deletes stored objects no metadata row references.

    An interrupted upload leaves exactly this: bytes written before the row
    that would have named them. The grace period keeps the reaper from racing
    an upload that is still in flight.
    """

    name = "orphan_reaper"

    def __init__(self, objects: ObjectStore, settings: Settings) -> None:
        self._objects = objects
        self._grace = timedelta(minutes=settings.orphan_grace_minutes)
        self._abandon = timedelta(hours=settings.s3_multipart_abandon_hours)
        self._batch = settings.page_size_max

    async def run(self, uow: UnitOfWork, *, now: datetime | None = None) -> ReaperResult:
        moment = now or utcnow()
        scanned = deleted = reclaimed = 0

        async for stored in self._objects.list_keys():
            scanned += 1
            if await self._is_referenced(uow, stored.key):
                continue
            if not self._is_old_enough(stored, moment):
                continue
            await self._objects.delete(stored.key)
            deleted += 1
            reclaimed += stored.size

        uploads, upload_bytes = await self._reap_abandoned_multipart(uow, moment)
        reclaimed += upload_bytes
        await uow.commit()

        job_runs_total.labels(job=self.name, outcome="success").inc()
        logger.info(
            "reaper_completed", scanned=scanned, deleted=deleted, bytes=reclaimed, uploads=uploads
        )
        return ReaperResult(scanned, deleted, reclaimed, uploads)

    async def _reap_abandoned_multipart(self, uow: UnitOfWork, now: datetime) -> tuple[int, int]:
        """Reclaim uploads left neither completed nor aborted past the window.

        Their staged part objects live outside the live-object key shape, so the
        scan above never touches them; this metadata-driven sweep deletes each
        part object and the upload's rows. Returns the count reclaimed and the
        bytes those parts held.
        """
        cutoff = now - self._abandon
        abandoned = await uow.multipart.list_abandoned_before(cutoff, limit=self._batch)
        reclaimed_bytes = 0
        for upload in abandoned:
            for part in await uow.multipart.list_parts(upload.upload_id):
                await self._objects.delete(part.object_key)
                reclaimed_bytes += part.size
            await uow.multipart.delete(upload.upload_id)
            # This upload never completed or aborted, so its create-time increment
            # is still outstanding; the reap is the third and final way out.
            s3_multipart_uploads_in_flight.dec()
        return len(abandoned), reclaimed_bytes

    @staticmethod
    async def _is_referenced(uow: UnitOfWork, key: str) -> bool:
        parsed = parse_object_key(key)
        if parsed is None:
            # Not a key CyberFS writes. Left alone rather than deleted -- the
            # reaper must never be the thing that removes someone else's data.
            return True
        _, version_id = parsed
        return await uow.versions.get(version_id) is not None

    def _is_old_enough(self, stored: StoredObject, now: datetime) -> bool:
        """Only reap objects older than the grace period.

        Without this, an upload in flight -- object written, row not yet
        committed -- would be collected out from under itself. An object whose
        age is unknown is left alone: deleting live data is far worse than
        leaving garbage for the next sweep.
        """
        if stored.last_modified is None:
            return False
        return now - stored.last_modified >= self._grace


class ReconcileQuotasJob:
    """Recomputes usage from the rows, correcting counter drift.

    Counters are an accelerator, and accelerators drift. `admin-dashboard/spec.md`
    requires reported figures to reconcile with metadata rather than displaying
    drift indefinitely.
    """

    name = "reconcile_quotas"

    def __init__(self, settings: Settings) -> None:
        self._batch = settings.page_size_max

    async def run(self, uow: UnitOfWork, *, now: datetime | None = None) -> ReconcileResult:
        moment = now or utcnow()
        page = await uow.users.list_all(limit=self._batch)

        checked = corrected = 0
        for user in page.items:
            checked += 1
            truth = await uow.quotas.recompute(user.id)
            usage = await uow.quotas.get(user.id)
            if usage is None:
                await uow.quotas.add(truth)
                corrected += 1
                continue
            if usage.reconcile(
                live_bytes=truth.live_bytes,
                trashed_bytes=truth.trashed_bytes,
                version_bytes=truth.version_bytes,
                now=moment,
            ):
                await uow.quotas.update(usage)
                corrected += 1
                logger.warning("quota_drift_corrected", user_id=str(user.id))

        await uow.commit()
        job_runs_total.labels(job=self.name, outcome="success").inc()
        return ReconcileResult(checked, corrected)


class ActivityPruneJob:
    """Deletes activity records past their retention.

    Only the activity actions are ever passed to the delete, so security
    records -- denials, grant changes, ownership transfers, encryption changes,
    admin actions -- survive under the longer audit retention. Losing an
    activity line is a shrug; losing an evidence line is not, so the two are
    pruned on different clocks and this job touches only the former.
    """

    name = "activity_prune"

    #: Frozen once, so the set of prunable actions is stable across a run.
    _ACTIONS: tuple[str, ...] = tuple(sorted(str(action) for action in ACTIVITY_ACTIONS))

    def __init__(self, settings: Settings) -> None:
        self._retention = timedelta(days=settings.activity_retention_days)

    async def run(self, uow: UnitOfWork, *, now: datetime | None = None) -> ActivityPruneResult:
        moment = now or utcnow()
        cutoff = moment - self._retention
        pruned = await uow.audit.prune_activity(cutoff, actions=self._ACTIONS)
        await uow.commit()
        job_runs_total.labels(job=self.name, outcome="success").inc()
        logger.info("activity_prune_completed", records=pruned)
        return ActivityPruneResult(pruned)


class RewrapJob:
    """Completes the deferred rewrap of large shared subtrees.

    Sharing a subtree with more encrypted nodes than `ASYNC_REWRAP_THRESHOLD_NODES`
    stores the grant *pending* and hands the DEK rewrap here. This worker walks
    each pending grant's whole subtree, rewrapping every encrypted descendant for
    the recipient (idempotently), then -- only after a clean pass confirms every
    encrypted node is now decryptable by the recipient -- flips the grant active
    and drops the recipient's cached permission decisions.

    Crash-safe and idempotent: each grant is completed in its own transaction, so
    an interruption leaves that grant pending (conferring nothing, since a pending
    grant is invisible) rather than half-usable, and a later run finishes it --
    `rewrap_for` never double-wraps an already-wrapped node.

    Create-during-pending gap: because a pending grant confers no access, every
    file created under the folder up to activation predates this worker's scan and
    is rewrapped before the grant becomes visible. Files created *after*
    activation fall under the same behaviour as any synchronous share (the create
    path mints only the owner's DEK) and are out of this job's scope.
    """

    name = "rewrap"

    def __init__(
        self,
        keys: KeyRewrapper,
        cache: PermissionInvalidator | None,
        settings: Settings,
    ) -> None:
        self._keys = keys
        self._cache = cache
        self._batch = settings.page_size_max

    async def run(self, uow: UnitOfWork, *, now: datetime | None = None) -> RewrapResult:
        moment = now or utcnow()
        pending = await uow.grants.list_pending(limit=self._batch)

        activated: list[str] = []
        nodes_rewrapped = 0
        for grant in pending:
            node = await uow.nodes.get(grant.node_id)
            if node is None or node.is_deleted:
                # The shared node vanished; drop the grant and its partial keys
                # rather than looping on it forever.
                await self._drop(uow, grant)
                await uow.commit()
                continue
            count = await self._activate_one(uow, grant, node, moment)
            if count is not None:
                nodes_rewrapped += count
                activated.append(grant.subject)
            await uow.commit()

        await self._invalidate(activated)
        job_runs_total.labels(job=self.name, outcome="success").inc()
        logger.info(
            "rewrap_completed",
            scanned=len(pending),
            activated=len(activated),
            nodes=nodes_rewrapped,
        )
        return RewrapResult(len(pending), len(activated), nodes_rewrapped)

    async def _activate_one(
        self, uow: UnitOfWork, grant: Grant, node: Node, now: datetime
    ) -> int | None:
        """Rewrap the whole subtree, then activate only on a clean pass.

        Returns the number of nodes rewrapped when the grant was activated, or
        None when a node could not be rewrapped (the grant stays pending and is
        retried next tick).
        """
        subject = grant.subject
        subtree = await self._subtree(uow, node)
        count = 0
        for item in subtree:
            if item.encrypted:
                await self._keys.rewrap_for(uow, item, subject, now)
                count += 1
        if not await self._fully_rewrapped(uow, subtree, subject):
            return None
        await uow.grants.update(grant.activated(now))
        return count

    async def _fully_rewrapped(
        self, uow: UnitOfWork, subtree: tuple[Node, ...], subject: str
    ) -> bool:
        """Every encrypted node in the subtree now has a key for the recipient."""
        for item in subtree:
            if item.encrypted and await uow.keys.get_data_key(item.id, subject) is None:
                return False
        return True

    async def _drop(self, uow: UnitOfWork, grant: Grant) -> None:
        await uow.grants.delete(grant.id)
        # Clean up any keys a partial rewrap left, so nothing lingers for a
        # subject who never gained access.
        await uow.keys.delete_data_key(grant.node_id, grant.subject)

    @staticmethod
    async def _subtree(uow: UnitOfWork, node: Node) -> tuple[Node, ...]:
        descendants = await uow.nodes.descendants(node.id, max_depth=ANCESTOR_GUARD_DEPTH)
        return (node, *descendants)

    async def _invalidate(self, subjects: list[str]) -> None:
        if self._cache is None:
            return
        for subject in subjects:
            await self._cache.invalidate_permissions_for_subject(subject)
