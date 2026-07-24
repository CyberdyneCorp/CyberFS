"""Durable backup history, on its own session.

Implements the `BackupRepository` port. Unlike the request repositories, this
one is not driven by a Unit of Work: a backup runs outside any request, so it
owns a session factory and opens a short transaction per call. That isolation
is deliberate -- a backup writing its progress must never share, or stall on, a
request's transaction.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cyberfs.adapters.outbound.db import mappers
from cyberfs.adapters.outbound.db import models as m
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.backup import BackupRecord, BackupState


class SqlBackupRepository:
    """Implements the `BackupRepository` port."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def add(self, record: BackupRecord) -> None:
        async with self._sessions() as session, session.begin():
            session.add(mappers.backup_record_to_row(record))

    async def update(self, record: BackupRecord) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(m.BackupRecordRow, record.id)
            if row is not None:
                mappers.apply_backup_record(row, record)

    async def get(self, backup_id: uuid.UUID) -> BackupRecord | None:
        async with self._sessions() as session:
            row = await session.get(m.BackupRecordRow, backup_id)
            return mappers.backup_record_from_row(row) if row is not None else None

    async def list_recent(self, *, days: int) -> tuple[BackupRecord, ...]:
        cutoff = utcnow() - timedelta(days=days)
        stmt = (
            select(m.BackupRecordRow)
            .where(m.BackupRecordRow.started_at >= cutoff)
            .order_by(m.BackupRecordRow.started_at.desc())
        )
        return await self._collect(stmt)

    async def list_verified(self) -> tuple[BackupRecord, ...]:
        stmt = (
            select(m.BackupRecordRow)
            .where(m.BackupRecordRow.state == str(BackupState.VERIFIED))
            .order_by(m.BackupRecordRow.finished_at.desc())
        )
        return await self._collect(stmt)

    async def latest_verified(self) -> BackupRecord | None:
        stmt = (
            select(m.BackupRecordRow)
            .where(m.BackupRecordRow.state == str(BackupState.VERIFIED))
            .order_by(m.BackupRecordRow.finished_at.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
            return mappers.backup_record_from_row(row) if row is not None else None

    async def _collect(self, stmt: Select[tuple[m.BackupRecordRow]]) -> tuple[BackupRecord, ...]:
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars()
            return tuple(mappers.backup_record_from_row(row) for row in rows)
