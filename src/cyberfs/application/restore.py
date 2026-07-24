"""The restore use case.

Takes a named backup and rebuilds a stack from it, through ports only. It loads
the dump behind `MetadataDump`, applies migrations up to the deployed head and
reports the upgrade path taken, mirrors objects from the backup store into the
target, and reports success only when an injected readiness probe passes.

Two guards shape it. Non-destructive by default: a target that already holds
data is refused unless the caller opts in. And key-aware degradation: if the
deployment `MASTER_KEY` does not open the restored key material, unencrypted
files stay readable but encrypted ones cannot be, so the service is reported not
healthy -- `backup-restore/spec.md`, "Restore without the key is partially
degraded".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from cyberfs.application.streaming import collect
from cyberfs.domain.backup import BackupManifest, BackupRecord
from cyberfs.domain.errors import ConflictError, NotFoundError
from cyberfs.domain.ports.backup import BackupRepository, MetadataDump
from cyberfs.domain.ports.storage import ObjectStore
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

#: A dependency probe returning whether some condition holds against the target
#: stack. Injected so the application stays free of Postgres/MinIO/crypto.
Probe = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """The outcome of a restore.

    `migrations_applied` is the ordered upgrade path carried on top of the
    dump's schema. `healthy` is false whenever the master key is unavailable,
    even when every object was restored -- an encrypted stack nobody can decrypt
    is not healthy.
    """

    backup_id: uuid.UUID
    schema_revision: str
    migrations_applied: tuple[str, ...]
    objects_restored: int
    key_available: bool
    healthy: bool


class RestoreService:
    def __init__(
        self,
        *,
        metadata_dump: MetadataDump,
        backup_store: ObjectStore,
        target_store: ObjectStore,
        repository: BackupRepository,
        readiness: Probe,
        key_available: Probe,
        target_is_empty: Probe,
    ) -> None:
        self._dump = metadata_dump
        self._backup = backup_store
        self._target = target_store
        self._repo = repository
        self._readiness = readiness
        self._key_available = key_available
        self._target_is_empty = target_is_empty

    async def restore(self, backup_id: uuid.UUID, *, destructive: bool = False) -> RestoreReport:
        record = await self._repo.get(backup_id)
        if record is None:
            raise NotFoundError("no such backup", backup_id=str(backup_id))
        await self._guard_target(destructive)

        manifest = await self._load_manifest(record)
        migrations = await self._dump.restore(
            self._backup.get(record.dump_key), target_revision=None
        )
        restored = await self._restore_objects(record, manifest)

        key_available = await self._key_available()
        healthy = key_available and await self._readiness()
        logger.info(
            "restore_completed",
            backup_id=str(backup_id),
            objects=restored,
            migrations=len(migrations),
            key_available=key_available,
            healthy=healthy,
        )
        return RestoreReport(
            backup_id=backup_id,
            schema_revision=record.schema_revision,
            migrations_applied=migrations,
            objects_restored=restored,
            key_available=key_available,
            healthy=healthy,
        )

    async def _guard_target(self, destructive: bool) -> None:
        """Refuse a target that already holds data unless explicitly forced."""
        if destructive:
            return
        if not await self._target_is_empty():
            raise ConflictError(
                "restore target is not empty; pass the destructive flag to overwrite it"
            )

    async def _load_manifest(self, record: BackupRecord) -> BackupManifest:
        raw = await collect(self._backup.get(record.manifest_key))
        return BackupManifest.from_json(raw.decode())

    async def _restore_objects(self, record: BackupRecord, manifest: BackupManifest) -> int:
        for entry in manifest.entries:
            await self._restore_one(record, entry.key)
        return len(manifest.entries)

    async def _restore_one(self, record: BackupRecord, key: str) -> None:
        """Copy one object back into the target, ciphertext untouched."""
        source: AsyncIterator[bytes] = self._backup.get(f"{record.bucket_prefix}/objects/{key}")
        await self._target.put(key, source)
