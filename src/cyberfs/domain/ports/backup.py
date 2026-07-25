"""Backup ports.

Protocols the backup pipeline talks to, owned by the domain and implemented by
outbound adapters. The Postgres dump/restore sits behind `MetadataDump` so the
service never shells out to `pg_dump` directly, and the durable history sits
behind `BackupRepository`. The backup *target* reuses the existing `ObjectStore`
port -- a second store pointed at `BACKUP_S3_*` -- so mirroring is a streamed
`get()` -> `put()` that copies ciphertext verbatim and never decrypts anything.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from cyberfs.domain.backup import BackupRecord


@dataclass(frozen=True, slots=True)
class DumpResult:
    """What a completed metadata dump reports about itself."""

    size: int
    checksum: str
    schema_revision: str


class BinarySink(Protocol):
    """A streaming write target for a dump, so it never lands in memory whole."""

    async def write(self, chunk: bytes) -> None: ...


class MetadataDump(Protocol):
    """A consistent Postgres dump and its restore, behind an interface.

    The concrete adapter shells out to `pg_dump`/`pg_restore`; keeping it a port
    means the application layer stays free of subprocess and DSN handling, and
    unit tests exercise the service with a fake.
    """

    async def dump(self, sink: BinarySink) -> DumpResult:
        """Stream a consistent snapshot of every application table to `sink`."""
        ...

    async def restore(
        self, source: AsyncIterator[bytes], *, target_revision: str | None = None
    ) -> tuple[str, ...]:
        """Load a dump, migrate up to `target_revision` (deployed head if None).

        Returns the ordered upgrade path -- the revisions applied on top of the
        dump's recorded schema to reach the target -- so the restore can report
        exactly how far the schema was carried forward.
        """
        ...

    async def current_revision(self) -> str:
        """The schema migration revision currently applied."""
        ...

    async def referenced_object_keys(self) -> frozenset[str]:
        """Every content object key the metadata references.

        The set difference against the mirror's manifest is what `detect_skew`
        turns into the dump/mirror skew reported on the backup record.
        """
        ...


class BackupRepository(Protocol):
    """Durable backup history.

    Standalone rather than folded into the request Unit of Work: a backup runs
    outside any request transaction, on its own session.
    """

    async def add(self, record: BackupRecord) -> None: ...
    async def update(self, record: BackupRecord) -> None: ...
    async def get(self, backup_id: uuid.UUID) -> BackupRecord | None: ...

    async def list_recent(self, *, days: int) -> tuple[BackupRecord, ...]:
        """History over the last `days`, including failures, newest first."""
        ...

    async def list_verified(self) -> tuple[BackupRecord, ...]:
        """Verified backups only -- the retention policy's input."""
        ...

    async def latest_verified(self) -> BackupRecord | None:
        """The most recent verified backup, for staleness checks."""
        ...
