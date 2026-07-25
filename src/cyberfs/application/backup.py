"""The backup use case.

Orchestrates one backup run from ports only: a consistent metadata dump behind
`MetadataDump`, a second `ObjectStore` pointed at the backup target, and a
`BackupRepository` for durable history. The pipeline dumps the schema, mirrors
every content object as ciphertext (`source.get()` -> `target.put()`, never a
decrypt -- `backup-restore/spec.md`, "Encrypted content stays encrypted"),
writes a manifest of keys/sizes/checksums, and verifies before marking the run
successful. A verification miss or any error fails the run, which then never
counts toward retention.

Nothing here touches subprocesses, MinIO, or SQLAlchemy: those live behind the
ports, so this stays unit-testable with fakes.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from datetime import datetime

from cyberfs.application.streaming import once
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.backup import (
    BackupManifest,
    BackupRecord,
    ManifestEntry,
    RetentionDecision,
    SkewReport,
    decide_retention,
    detect_skew,
)
from cyberfs.domain.errors import IntegrityFailureError
from cyberfs.domain.ports.backup import BackupRepository, DumpResult, MetadataDump
from cyberfs.domain.ports.storage import ObjectStore, StoredObject
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

Clock = Callable[[], datetime]

#: Bounds how many dump chunks sit between the producer (pg_dump) and the
#: consumer (the upload) at once, so the bridge never buffers the whole dump.
_BRIDGE_DEPTH = 8


class _StreamBridge:
    """Adapts a push-based `BinarySink` to a pull-based upload stream.

    `MetadataDump.dump` writes chunks; `ObjectStore.put` wants to pull them.
    A bounded queue joins the two so the dump streams straight into the upload
    without the whole thing ever landing in memory.
    """

    def __init__(self, depth: int = _BRIDGE_DEPTH) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=depth)

    async def write(self, chunk: bytes) -> None:
        await self._queue.put(chunk)

    async def close(self) -> None:
        await self._queue.put(None)

    async def stream(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk


class BackupService:
    """Runs, verifies, prunes, and lists backups."""

    def __init__(
        self,
        *,
        metadata_dump: MetadataDump,
        source_store: ObjectStore,
        backup_store: ObjectStore,
        repository: BackupRepository,
        keep_daily: int,
        keep_weekly: int,
        keep_monthly: int,
        failed_grace_hours: int,
        verify_sample_count: int,
        history_days: int,
        clock: Clock = utcnow,
    ) -> None:
        self._dump = metadata_dump
        self._source = source_store
        self._backup = backup_store
        self._repo = repository
        self._keep_daily = keep_daily
        self._keep_weekly = keep_weekly
        self._keep_monthly = keep_monthly
        self._failed_grace_hours = failed_grace_hours
        self._verify_sample_count = verify_sample_count
        self._history_days = history_days
        self._clock = clock

    # --- run -----------------------------------------------------------

    async def run(self, *, now: datetime | None = None) -> BackupRecord:
        """Execute one backup, returning the persisted record either way."""
        moment = now or self._clock()
        revision = await self._dump.current_revision()
        record = BackupRecord.start(started_at=moment, schema_revision=revision)
        await self._repo.add(record)
        logger.info("backup_started", backup_id=str(record.id), revision=revision)

        try:
            manifest, skew = await self._copy(record)
            await self.verify(record, manifest)
        except Exception as exc:
            return await self._fail(record, exc)

        verified = record.mark_verified(finished_at=self._clock(), manifest=manifest, skew=skew)
        await self._repo.update(verified)
        logger.info(
            "backup_verified",
            backup_id=str(record.id),
            objects=manifest.object_count,
            bytes=manifest.total_bytes,
            skew=skew.has_skew,
        )
        return verified

    async def _fail(self, record: BackupRecord, exc: Exception) -> BackupRecord:
        failed = record.mark_failed(finished_at=self._clock(), error=type(exc).__name__)
        await self._repo.update(failed)
        logger.error("backup_failed", backup_id=str(record.id), error=type(exc).__name__)
        return failed

    async def _copy(self, record: BackupRecord) -> tuple[BackupManifest, SkewReport]:
        dump_result = await self._dump_metadata(record)
        entries, in_flight = await self._mirror(record)
        manifest = BackupManifest.from_entries(
            entries,
            dump_checksum=dump_result.checksum,
            dump_size=dump_result.size,
            schema_revision=record.schema_revision,
        )
        await self._upload_bytes(
            record.manifest_key, manifest.to_json().encode(), "application/json"
        )
        referenced = await self._dump.referenced_object_keys()
        skew = detect_skew(
            frozenset(entry.key for entry in entries),
            referenced,
            in_flight=in_flight,
        )
        return manifest, skew

    async def _dump_metadata(self, record: BackupRecord) -> DumpResult:
        """Stream the metadata dump straight into the backup store."""
        bridge = _StreamBridge()

        async def _pump() -> DumpResult:
            try:
                return await self._dump.dump(bridge)
            finally:
                await bridge.close()

        task = asyncio.create_task(_pump())
        try:
            await self._backup.put(
                record.dump_key, bridge.stream(), content_type="application/gzip"
            )
        finally:
            # Even if the upload fails, let the dump finish (or surface its error)
            # rather than leaking the task.
            result = await task
        return result

    async def _mirror(
        self, record: BackupRecord
    ) -> tuple[tuple[ManifestEntry, ...], frozenset[str]]:
        entries: list[ManifestEntry] = []
        in_flight: set[str] = set()
        async for stored in self._source.list_keys():
            entries.append(await self._mirror_one(record, stored.key))
            if _is_in_flight(stored, record.started_at):
                in_flight.add(stored.key)
        return tuple(entries), frozenset(in_flight)

    async def _mirror_one(self, record: BackupRecord, key: str) -> ManifestEntry:
        """Copy one object verbatim, hashing its bytes as they pass through.

        The bytes are never decrypted -- an encrypted object is mirrored as the
        ciphertext it already is.
        """
        hasher = hashlib.sha256()

        async def _tee() -> AsyncIterator[bytes]:
            async for chunk in self._source.get(key):
                hasher.update(chunk)
                yield chunk

        size = await self._backup.put(_object_key(record, key), _tee())
        return ManifestEntry(key=key, size=size, checksum=hasher.hexdigest())

    # --- verify --------------------------------------------------------

    async def verify(self, record: BackupRecord, manifest: BackupManifest) -> None:
        """Reconfirm the dump checksum and sample objects against the manifest.

        Any mismatch raises, which fails the backup so it never counts toward
        retention -- `backup-restore/spec.md`, "Mismatch fails the backup".
        """
        if await self._checksum(record.dump_key) != manifest.dump_checksum:
            raise IntegrityFailureError("backup dump checksum did not match the manifest")
        sample = manifest.sample(self._verify_sample_count, seed=str(record.id))
        for entry in sample:
            actual = await self._checksum(_object_key(record, entry.key))
            if actual != entry.checksum:
                raise IntegrityFailureError("mirrored object checksum did not match the manifest")

    async def _checksum(self, key: str) -> str:
        hasher = hashlib.sha256()
        async for chunk in self._backup.get(key):
            hasher.update(chunk)
        return hasher.hexdigest()

    # --- retention -----------------------------------------------------

    async def apply_retention(self, *, now: datetime | None = None) -> RetentionDecision:
        """Prune backups per the policy, protecting the last verified one."""
        moment = now or self._clock()
        history = await self._repo.list_recent(days=self._history_days)
        decision = decide_retention(
            history,
            keep_daily=self._keep_daily,
            keep_weekly=self._keep_weekly,
            keep_monthly=self._keep_monthly,
            now=moment,
            failed_grace_hours=self._failed_grace_hours,
        )
        if decision.protected_last_verified:
            logger.warning("backup_retention_protected_last_verified")
        for backup_id in decision.delete:
            await self._delete_artifacts(f"backups/{backup_id}")
        logger.info(
            "backup_retention_applied",
            kept=len(decision.keep),
            deleted=len(decision.delete),
        )
        return decision

    async def _delete_artifacts(self, prefix: str) -> None:
        async for stored in self._backup.list_keys(f"{prefix}/"):
            await self._backup.delete(stored.key)

    # --- listing -------------------------------------------------------

    async def list_backups(self) -> tuple[BackupRecord, ...]:
        """History over the retention window, newest first, failures included."""
        return await self._repo.list_recent(days=self._history_days)

    # --- helpers -------------------------------------------------------

    async def _upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        await self._backup.put(key, once(data), content_type=content_type)


def _object_key(record: BackupRecord, key: str) -> str:
    """Where a mirrored content object lives inside a backup's prefix."""
    return f"{record.bucket_prefix}/objects/{key}"


def _is_in_flight(stored: StoredObject, started_at: datetime) -> bool:
    """Whether an object landed after the run began -- tolerated drift.

    An upload that commits mid-backup is expected to appear in the mirror but
    not the dump (or the reverse); it is not corruption, so skew detection
    ignores it.
    """
    if stored.last_modified is None:
        return False
    return stored.last_modified >= started_at
