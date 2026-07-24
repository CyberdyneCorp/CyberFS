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

from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.ports.storage import ObjectStore, StoredObject
from cyberfs.infrastructure.logging import get_logger
from cyberfs.infrastructure.metrics import job_runs_total
from cyberfs.infrastructure.settings import Settings

logger = get_logger(__name__)

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


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    users_checked: int = 0
    users_corrected: int = 0


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

        job_runs_total.labels(job=self.name, outcome="success").inc()
        logger.info("reaper_completed", scanned=scanned, deleted=deleted, bytes=reclaimed)
        return ReaperResult(scanned, deleted, reclaimed)

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
