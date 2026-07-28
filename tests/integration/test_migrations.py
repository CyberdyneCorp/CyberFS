"""The Alembic migration, applied to a real database.

`deployment/spec.md` requires migrations to run before the port binds and to
be serialized across replicas. What is checked here is that the migration
actually produces the schema the models expect -- including the partial unique
index the trash design depends on, which a naive autogenerate would drop.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine

from cyberfs.adapters.outbound.db.models import Base
from cyberfs.infrastructure.db import MIGRATION_LOCK_ID, advisory_lock

from .conftest import database_url

REPO_ROOT = Path(__file__).resolve().parents[2]

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


# --- rollback -------------------------------------------------------------
#
# `docs/outstanding-verification.md` recorded for a long time that a `downgrade`
# was written for every migration and exercised for none, so the rollback plan in
# each design document was a claim rather than a fact. These run the whole chain
# in both directions.


def _alembic_config(url: str) -> Config:
    """Alembic pointed at a throwaway database, with the sync driver.

    Alembic's `env.py` runs its own engine, and the migrations are synchronous, so
    the asyncpg URL the rest of the suite uses has to be handed over as psycopg.
    """
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("+asyncpg", "+psycopg"))
    return config


@pytest.fixture
def scratch_database() -> Iterator[str]:
    """A database of its own, created and dropped around the test.

    Not the shared test database: walking it down to base would drop every table
    the rest of the suite depends on, and `CREATE DATABASE` cannot run inside a
    transaction, hence `AUTOCOMMIT`.
    """
    name = f"cyberfs_rollback_{uuid.uuid4().hex[:10]}"
    admin_url = database_url().replace("+asyncpg", "+psycopg")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield admin_url.rsplit("/", 1)[0] + "/" + name
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def test_every_migration_downgrades_and_upgrades_again(scratch_database: str) -> None:
    """The whole chain, down to base and back up.

    One test rather than one per revision, because a rollback is only meaningful
    as a sequence: a `downgrade` that leaves a stray constraint behind fails on
    the way back up, not where it was written.
    """
    config = _alembic_config(scratch_database)

    command.upgrade(config, "head")
    tables_before = _table_names(scratch_database)
    assert EXPECTED_TABLES <= tables_before

    command.downgrade(config, "base")

    remaining = _table_names(scratch_database) - {"alembic_version"}
    assert remaining == set(), f"downgrade left tables behind: {sorted(remaining)}"

    command.upgrade(config, "head")

    assert _table_names(scratch_database) == tables_before, (
        "the schema after down-then-up differs from the schema before"
    )


def test_a_single_step_back_and_forward_keeps_the_newest_column(
    scratch_database: str,
) -> None:
    """One step, targeting the most recent migration specifically.

    The full walk above would pass even if the newest `downgrade` were a no-op,
    since a later `upgrade` of an already-present column raises. This checks the
    column actually goes and comes back.
    """
    config = _alembic_config(scratch_database)
    command.upgrade(config, "head")
    assert "seal_version_id" in _column_names(scratch_database, "file_versions")

    command.downgrade(config, "-1")
    assert "seal_version_id" not in _column_names(scratch_database, "file_versions")

    command.upgrade(config, "head")
    assert "seal_version_id" in _column_names(scratch_database, "file_versions")


def test_the_backfill_leaves_existing_rows_sealed_under_their_own_id(
    scratch_database: str,
) -> None:
    """The migration's central claim, on rows that predate it.

    Content sealed before the column existed was sealed in place, so its sealing
    id is its own id. Seeded at the previous revision -- where the column does not
    exist -- so the row is genuinely older than the migration rather than merely
    written to look that way.
    """
    config = _alembic_config(scratch_database)
    command.downgrade(config, "base")
    command.upgrade(config, "b7e3c9a1d2f5")  # the revision before the sealing id

    url = scratch_database
    engine = create_engine(url)
    node_id, owner_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, subject, root_folder_id, quota_bytes, is_admin, "
                "created_at, updated_at) VALUES (:owner, 'legacy', :node, 0, false, now(), now())"
            ),
            {"owner": owner_id, "node": node_id},
        )
        connection.execute(
            text(
                "INSERT INTO nodes (id, owner_id, parent_id, kind, name, normalized_name, "
                "revision, created_at, updated_at, size_bytes, encrypted, encryption_default) "
                "VALUES (:node, :owner, NULL, 'file', 'old.bin', 'old.bin', 1, now(), now(), "
                "10, true, 'inherit')"
            ),
            {"node": node_id, "owner": owner_id},
        )
        connection.execute(
            text(
                "INSERT INTO file_versions (id, node_id, owner_id, sequence, size_bytes, "
                "plaintext_digest, content_type, encrypted, created_at, created_by) VALUES "
                "(:vid, :node, :owner, 1, 10, 'deadbeef', 'application/octet-stream', true, "
                "now(), 'legacy')"
            ),
            {"vid": version_id, "node": node_id, "owner": owner_id},
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        sealed = connection.execute(
            text("SELECT seal_version_id FROM file_versions WHERE id = :vid"),
            {"vid": version_id},
        ).scalar_one()
    engine.dispose()
    assert sealed == version_id, "the backfill must leave old content sealed under its own id"


def _table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def _column_names(url: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table},
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()
