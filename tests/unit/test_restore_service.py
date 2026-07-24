"""Unit tests for the restore use case, against faked ports."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from cyberfs.application.restore import RestoreService
from cyberfs.domain.backup import BackupManifest, BackupRecord, ManifestEntry, SkewReport
from cyberfs.domain.errors import ConflictError, NotFoundError
from tests.unit.fakes import FakeObjectStore, stream
from tests.unit.test_backup_service import NOW, FakeBackupRepository


class FakeMetadataDumpRestore:
    def __init__(self, *, path: tuple[str, ...] = ()) -> None:
        self._path = path
        self.restored: list[bytes] = []

    async def dump(self, sink: object) -> object:  # pragma: no cover - unused here
        raise NotImplementedError

    async def restore(
        self, source: AsyncIterator[bytes], *, target_revision: str | None = None
    ) -> tuple[str, ...]:
        buffer = bytearray()
        async for chunk in source:
            buffer.extend(chunk)
        self.restored.append(bytes(buffer))
        return self._path

    async def current_revision(self) -> str:  # pragma: no cover - unused here
        return "079de84e4f44"

    async def referenced_object_keys(self) -> frozenset[str]:  # pragma: no cover
        return frozenset()


async def _seed_backup(
    backup: FakeObjectStore, repo: FakeBackupRepository, contents: dict[str, bytes]
) -> BackupRecord:
    record = BackupRecord.start(started_at=NOW, schema_revision="079de84e4f44")
    entries = tuple(ManifestEntry(key=k, size=len(v), checksum="x") for k, v in contents.items())
    manifest = BackupManifest.from_entries(
        entries, dump_checksum="d", dump_size=8, schema_revision="079de84e4f44"
    )
    verified = record.mark_verified(finished_at=NOW, manifest=manifest, skew=SkewReport())
    repo.records[verified.id] = verified
    await backup.put(verified.dump_key, stream(b"dumpdata"))
    await backup.put(verified.manifest_key, stream(manifest.to_json().encode()))
    for key, blob in contents.items():
        await backup.put(f"{verified.bucket_prefix}/objects/{key}", stream(blob))
    return verified


def _service(
    *,
    backup: FakeObjectStore,
    target: FakeObjectStore,
    repo: FakeBackupRepository,
    dump: FakeMetadataDumpRestore,
    readiness: bool = True,
    key_available: bool = True,
    empty: bool = True,
) -> RestoreService:
    async def _readiness() -> bool:
        return readiness

    async def _key() -> bool:
        return key_available

    async def _empty() -> bool:
        return empty

    return RestoreService(
        metadata_dump=dump,
        backup_store=backup,
        target_store=target,
        repository=repo,
        readiness=_readiness,
        key_available=_key,
        target_is_empty=_empty,
    )


@pytest.mark.asyncio
async def test_restore_loads_dump_and_mirrors_objects() -> None:
    backup, target, repo = FakeObjectStore(), FakeObjectStore(), FakeBackupRepository()
    contents = {"o1/n1/v1": b"encrypted-bytes", "o1/n2/v2": b"plain-bytes"}
    record = await _seed_backup(backup, repo, contents)
    dump = FakeMetadataDumpRestore(path=("b1f7c0a2d3e4",))
    service = _service(backup=backup, target=target, repo=repo, dump=dump)

    report = await service.restore(record.id)

    assert dump.restored == [b"dumpdata"]
    assert report.migrations_applied == ("b1f7c0a2d3e4",)
    assert report.objects_restored == 2
    assert report.key_available and report.healthy
    for key, blob in contents.items():
        assert target.objects[key] == blob


@pytest.mark.asyncio
async def test_non_empty_target_is_refused_without_destructive() -> None:
    backup, target, repo = FakeObjectStore(), FakeObjectStore(), FakeBackupRepository()
    record = await _seed_backup(backup, repo, {"o1/n1/v1": b"x"})
    dump = FakeMetadataDumpRestore()
    service = _service(backup=backup, target=target, repo=repo, dump=dump, empty=False)

    with pytest.raises(ConflictError):
        await service.restore(record.id)

    # Nothing was loaded or mirrored.
    assert dump.restored == []
    assert target.objects == {}


@pytest.mark.asyncio
async def test_destructive_overrides_non_empty_target() -> None:
    backup, target, repo = FakeObjectStore(), FakeObjectStore(), FakeBackupRepository()
    record = await _seed_backup(backup, repo, {"o1/n1/v1": b"x"})
    service = _service(
        backup=backup, target=target, repo=repo, dump=FakeMetadataDumpRestore(), empty=False
    )

    report = await service.restore(record.id, destructive=True)

    assert report.objects_restored == 1


@pytest.mark.asyncio
async def test_missing_backup_raises_not_found() -> None:
    service = _service(
        backup=FakeObjectStore(),
        target=FakeObjectStore(),
        repo=FakeBackupRepository(),
        dump=FakeMetadataDumpRestore(),
    )

    with pytest.raises(NotFoundError):
        await service.restore(uuid.uuid4())


@pytest.mark.asyncio
async def test_key_unavailable_keeps_objects_but_reports_not_healthy() -> None:
    backup, target, repo = FakeObjectStore(), FakeObjectStore(), FakeBackupRepository()
    contents = {"o1/n1/v1": b"encrypted", "o1/n2/v2": b"plain"}
    record = await _seed_backup(backup, repo, contents)
    service = _service(
        backup=backup, target=target, repo=repo, dump=FakeMetadataDumpRestore(), key_available=False
    )

    report = await service.restore(record.id)

    # Unencrypted files still land; the key gap only degrades health.
    assert report.objects_restored == 2
    assert target.objects["o1/n2/v2"] == b"plain"
    assert not report.key_available
    assert not report.healthy


@pytest.mark.asyncio
async def test_readiness_failure_reports_not_healthy() -> None:
    backup, target, repo = FakeObjectStore(), FakeObjectStore(), FakeBackupRepository()
    record = await _seed_backup(backup, repo, {"o1/n1/v1": b"x"})
    service = _service(
        backup=backup, target=target, repo=repo, dump=FakeMetadataDumpRestore(), readiness=False
    )

    report = await service.restore(record.id)

    assert report.key_available
    assert not report.healthy
