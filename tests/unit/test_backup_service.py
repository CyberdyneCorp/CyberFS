"""Unit tests for the backup use case, against faked ports."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.backup import BackupService
from cyberfs.domain.backup import BackupManifest, BackupRecord, BackupState, SkewReport
from cyberfs.domain.ports.backup import BinarySink, DumpResult
from tests.unit.fakes import FakeObjectStore, stream

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


class FakeMetadataDump:
    def __init__(
        self,
        *,
        payload: bytes = b"PGDUMPDATA",
        revision: str = "079de84e4f44",
        referenced: frozenset[str] = frozenset(),
        checksum: str | None = None,
    ) -> None:
        self._payload = payload
        self._revision = revision
        self._referenced = referenced
        self._checksum = checksum or hashlib.sha256(payload).hexdigest()
        self.restored: list[bytes] = []

    async def dump(self, sink: BinarySink) -> DumpResult:
        await sink.write(self._payload)
        return DumpResult(
            size=len(self._payload), checksum=self._checksum, schema_revision=self._revision
        )

    async def restore(
        self, source: AsyncIterator[bytes], *, target_revision: str | None = None
    ) -> tuple[str, ...]:
        buffer = bytearray()
        async for chunk in source:
            buffer.extend(chunk)
        self.restored.append(bytes(buffer))
        return ()

    async def current_revision(self) -> str:
        return self._revision

    async def referenced_object_keys(self) -> frozenset[str]:
        return self._referenced


class FakeBackupRepository:
    def __init__(self) -> None:
        self.records: dict[uuid.UUID, BackupRecord] = {}

    async def add(self, record: BackupRecord) -> None:
        self.records[record.id] = record

    async def update(self, record: BackupRecord) -> None:
        self.records[record.id] = record

    async def get(self, backup_id: uuid.UUID) -> BackupRecord | None:
        return self.records.get(backup_id)

    async def list_recent(self, *, days: int) -> tuple[BackupRecord, ...]:
        return tuple(sorted(self.records.values(), key=lambda r: r.started_at, reverse=True))

    async def list_verified(self) -> tuple[BackupRecord, ...]:
        return tuple(r for r in self.records.values() if r.is_verified)

    async def latest_verified(self) -> BackupRecord | None:
        verified = [r for r in self.records.values() if r.is_verified]
        return max(verified, key=lambda r: r.finished_at or NOW) if verified else None


def _service(
    *,
    source: FakeObjectStore,
    backup: FakeObjectStore,
    dump: FakeMetadataDump,
    repo: FakeBackupRepository,
    verify_sample_count: int = 50,
    clock_values: list[datetime] | None = None,
) -> BackupService:
    values = list(clock_values or [NOW])

    def _clock() -> datetime:
        return values.pop(0) if len(values) > 1 else values[0]

    return BackupService(
        metadata_dump=dump,
        source_store=source,
        backup_store=backup,
        repository=repo,
        keep_daily=7,
        keep_weekly=4,
        keep_monthly=6,
        failed_grace_hours=24,
        verify_sample_count=verify_sample_count,
        history_days=90,
        clock=_clock,
    )


async def _seed_source(store: FakeObjectStore, contents: dict[str, bytes]) -> None:
    for key, blob in contents.items():
        await store.put(key, stream(blob))


@pytest.mark.asyncio
async def test_run_verifies_and_mirrors_ciphertext_verbatim() -> None:
    source = FakeObjectStore()
    backup = FakeObjectStore()
    contents = {"o1/n1/v1": b"ciphertext-one", "o1/n2/v2": b"plaintext-two"}
    await _seed_source(source, contents)
    dump = FakeMetadataDump(referenced=frozenset(contents))
    repo = FakeBackupRepository()
    service = _service(source=source, backup=backup, dump=dump, repo=repo)

    record = await service.run(now=NOW)

    assert record.state is BackupState.VERIFIED
    assert record.object_count == 2
    # Objects are mirrored byte-for-byte -- never decrypted.
    for key, blob in contents.items():
        assert backup.objects[f"{record.bucket_prefix}/objects/{key}"] == blob
    # Dump and manifest landed in the backup store.
    assert backup.objects[record.dump_key] == b"PGDUMPDATA"
    manifest = BackupManifest.from_json(backup.objects[record.manifest_key].decode())
    assert {e.key for e in manifest.entries} == set(contents)
    assert manifest.dump_checksum == hashlib.sha256(b"PGDUMPDATA").hexdigest()


@pytest.mark.asyncio
async def test_run_records_dump_checksum_matching_stored_bytes() -> None:
    source = FakeObjectStore()
    backup = FakeObjectStore()
    dump = FakeMetadataDump(payload=b"consistent-snapshot")
    service = _service(source=source, backup=backup, dump=dump, repo=FakeBackupRepository())

    record = await service.run(now=NOW)

    assert record.dump_checksum == hashlib.sha256(b"consistent-snapshot").hexdigest()


@pytest.mark.asyncio
async def test_dump_checksum_mismatch_fails_the_backup() -> None:
    source = FakeObjectStore()
    backup = FakeObjectStore()
    # Checksum the adapter reports disagrees with the bytes it actually wrote.
    dump = FakeMetadataDump(payload=b"real-bytes", checksum="deadbeef")
    repo = FakeBackupRepository()
    service = _service(source=source, backup=backup, dump=dump, repo=repo)

    record = await service.run(now=NOW)

    assert record.state is BackupState.FAILED
    assert record.error == "IntegrityFailureError"
    assert repo.records[record.id].is_failed


class _CorruptingStore(FakeObjectStore):
    """Returns tampered bytes for content objects, so verification fails."""

    async def get(
        self, key: str, *, offset: int = 0, length: int | None = None
    ) -> AsyncIterator[bytes]:
        if "/objects/" in key:
            yield b"tampered"
            return
        async for chunk in super().get(key, offset=offset, length=length):
            yield chunk


@pytest.mark.asyncio
async def test_object_checksum_mismatch_fails_the_backup() -> None:
    source = FakeObjectStore()
    await _seed_source(source, {"o1/n1/v1": b"original"})
    backup = _CorruptingStore()
    service = _service(
        source=source,
        backup=backup,
        dump=FakeMetadataDump(),
        repo=FakeBackupRepository(),
        verify_sample_count=10,
    )

    record = await service.run(now=NOW)

    assert record.state is BackupState.FAILED
    assert record.error == "IntegrityFailureError"


@pytest.mark.asyncio
async def test_skew_between_dump_and_mirror_is_recorded() -> None:
    source = FakeObjectStore()
    await _seed_source(source, {"o1/n1/v1": b"a", "o1/n2/v2": b"b"})
    # The dump references a third object the mirror never saw, and omits v2.
    dump = FakeMetadataDump(referenced=frozenset({"o1/n1/v1", "o1/n3/v3"}))
    service = _service(
        source=source, backup=FakeObjectStore(), dump=dump, repo=FakeBackupRepository()
    )

    record = await service.run(now=NOW)

    assert record.has_skew
    assert record.skew_missing_in_dump == 1  # v2 mirrored, not referenced
    assert record.skew_missing_in_manifest == 1  # v3 referenced, not mirrored


@pytest.mark.asyncio
async def test_in_flight_objects_are_not_counted_as_skew() -> None:
    source = FakeObjectStore()
    await _seed_source(source, {"o1/n1/v1": b"a", "o1/n2/v2": b"b"})
    # v2 landed after the run began: tolerated in-flight drift, not skew.
    source.timestamps["o1/n2/v2"] = NOW + timedelta(seconds=5)
    dump = FakeMetadataDump(referenced=frozenset({"o1/n1/v1"}))
    service = _service(
        source=source, backup=FakeObjectStore(), dump=dump, repo=FakeBackupRepository()
    )

    record = await service.run(now=NOW)

    assert not record.has_skew


@pytest.mark.asyncio
async def test_apply_retention_prunes_old_artifacts_and_protects_last_verified() -> None:
    backup = FakeObjectStore()
    repo = FakeBackupRepository()
    old = _verified(started=NOW - timedelta(days=40), finished=NOW - timedelta(days=40))
    recent = _verified(started=NOW - timedelta(days=1), finished=NOW - timedelta(days=1))
    repo.records[old.id] = old
    repo.records[recent.id] = recent
    await backup.put(f"{old.bucket_prefix}/dump.sql.gz", stream(b"old-dump"))
    await backup.put(f"{recent.bucket_prefix}/dump.sql.gz", stream(b"new-dump"))

    service = BackupService(
        metadata_dump=FakeMetadataDump(),
        source_store=FakeObjectStore(),
        backup_store=backup,
        repository=repo,
        keep_daily=0,
        keep_weekly=0,
        keep_monthly=0,
        failed_grace_hours=24,
        verify_sample_count=1,
        history_days=90,
        clock=lambda: NOW,
    )

    decision = await service.apply_retention(now=NOW)

    assert decision.protected_last_verified
    assert old.id in decision.delete
    assert recent.id in decision.keep
    assert f"{old.bucket_prefix}/dump.sql.gz" not in backup.objects
    assert f"{recent.bucket_prefix}/dump.sql.gz" in backup.objects


@pytest.mark.asyncio
async def test_apply_retention_keeps_bucket_winner_without_protection() -> None:
    backup = FakeObjectStore()
    repo = FakeBackupRepository()
    older = _verified(started=NOW - timedelta(days=2), finished=NOW - timedelta(days=2))
    newer = _verified(started=NOW - timedelta(days=1), finished=NOW - timedelta(days=1))
    repo.records[older.id] = older
    repo.records[newer.id] = newer
    await backup.put(f"{older.bucket_prefix}/dump.sql.gz", stream(b"old"))

    service = BackupService(
        metadata_dump=FakeMetadataDump(),
        source_store=FakeObjectStore(),
        backup_store=backup,
        repository=repo,
        keep_daily=1,
        keep_weekly=0,
        keep_monthly=0,
        failed_grace_hours=24,
        verify_sample_count=1,
        history_days=90,
        clock=lambda: NOW,
    )

    decision = await service.apply_retention(now=NOW)

    # The daily bucket keeps the newest on its own; protection never engages.
    assert not decision.protected_last_verified
    assert newer.id in decision.keep
    assert older.id in decision.delete


@pytest.mark.asyncio
async def test_list_backups_returns_history_newest_first() -> None:
    repo = FakeBackupRepository()
    first = _verified(started=NOW - timedelta(days=2), finished=NOW - timedelta(days=2))
    second = _verified(started=NOW - timedelta(days=1), finished=NOW - timedelta(days=1))
    repo.records[first.id] = first
    repo.records[second.id] = second
    service = _service(
        source=FakeObjectStore(), backup=FakeObjectStore(), dump=FakeMetadataDump(), repo=repo
    )

    listed = await service.list_backups()

    assert [r.id for r in listed] == [second.id, first.id]


def _verified(*, started: datetime, finished: datetime) -> BackupRecord:
    record = BackupRecord.start(started_at=started, schema_revision="079de84e4f44")
    manifest = BackupManifest.from_entries(
        (), dump_checksum="sum", dump_size=1, schema_revision="079de84e4f44"
    )
    return record.mark_verified(finished_at=finished, manifest=manifest, skew=SkewReport())
