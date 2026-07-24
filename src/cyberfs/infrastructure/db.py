"""Engine, session factory, and the migration runner.

`deployment/spec.md` requires migrations to be applied before the port binds,
serialized across replicas by a lock, and to exit non-zero on failure rather
than serve traffic against a half-migrated schema.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cyberfs.infrastructure.logging import get_logger
from cyberfs.infrastructure.settings import Settings

logger = get_logger(__name__)

#: Arbitrary but fixed: every replica takes the same Postgres advisory lock, so
#: exactly one applies migrations while the others wait.
MIGRATION_LOCK_ID = 0x0C_1BE2_F5


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def advisory_lock(engine: AsyncEngine, lock_id: int) -> AsyncIterator[None]:
    """Hold a Postgres session-level advisory lock.

    `pg_advisory_lock` blocks until granted, which is what we want: a replica
    that loses the race should wait for the winner to finish migrating, not
    give up and start serving against an old schema.
    """
    async with engine.connect() as connection:
        await connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": lock_id})
        try:
            yield
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
