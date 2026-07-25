"""The Alembic migration, applied to a real database.

`deployment/spec.md` requires migrations to run before the port binds and to
be serialized across replicas. What is checked here is that the migration
actually produces the schema the models expect -- including the partial unique
index the trash design depends on, which a naive autogenerate would drop.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cyberfs.adapters.outbound.db.models import Base
from cyberfs.infrastructure.db import MIGRATION_LOCK_ID, advisory_lock

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "users",
    "nodes",
    "file_versions",
    "grants",
    "public_links",
    "user_keys",
    "wrapped_data_keys",
    "quota_usage",
    "audit_records",
}


async def test_every_model_table_exists(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        tables = {row[0] for row in result}
    assert EXPECTED_TABLES <= tables


async def test_model_metadata_matches_the_database(engine: AsyncEngine) -> None:
    """Every mapped table is present under the name the models declare."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        tables = {row[0] for row in result}
    assert set(Base.metadata.tables) <= tables


async def test_sibling_name_index_is_partial(engine: AsyncEngine) -> None:
    """Without the WHERE clause, a trashed file would hold its name forever."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'nodes' AND indexname = 'uq_nodes_parent_name_live'"
            )
        )
        definition = result.scalar_one()
    assert "UNIQUE" in definition
    assert "deleted_at IS NULL" in definition


async def test_grant_uniqueness_is_per_node_and_subject(engine: AsyncEngine) -> None:
    """A regrant must replace a role, never accumulate a second row."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'grants'::regclass AND contype = 'u'"
            )
        )
        names = {row[0] for row in result}
    assert "uq_grants_node_subject" in names


async def test_wrapped_key_uniqueness_is_per_node_and_subject(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'wrapped_data_keys'::regclass AND contype = 'u'"
            )
        )
        names = {row[0] for row in result}
    assert "uq_wrapped_data_keys_node_subject" in names


async def test_timestamps_are_timezone_aware(engine: AsyncEngine) -> None:
    """Naive timestamps would make retention windows wrong across zones."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'nodes' AND column_name = 'created_at'"
            )
        )
        assert result.scalar_one() == "timestamp with time zone"


async def test_migration_lock_serializes_holders(engine: AsyncEngine) -> None:
    """Two replicas starting together must not migrate concurrently."""
    order: list[str] = []
    released = asyncio.Event()

    async def first() -> None:
        async with advisory_lock(engine, MIGRATION_LOCK_ID):
            order.append("first-acquired")
            await asyncio.sleep(0.15)
            order.append("first-released")
        released.set()

    async def second() -> None:
        await asyncio.sleep(0.05)
        async with advisory_lock(engine, MIGRATION_LOCK_ID):
            order.append("second-acquired")

    await asyncio.gather(first(), second())

    assert order == ["first-acquired", "first-released", "second-acquired"]


async def test_lock_is_released_even_when_the_body_raises(engine: AsyncEngine) -> None:
    with pytest.raises(RuntimeError):
        async with advisory_lock(engine, MIGRATION_LOCK_ID):
            raise RuntimeError("migration blew up")

    # Re-acquirable, so a failed migration does not wedge every later boot.
    async with advisory_lock(engine, MIGRATION_LOCK_ID):
        pass
