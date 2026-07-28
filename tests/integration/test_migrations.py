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

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cyberfs.adapters.outbound.db.models import Base
from cyberfs.infrastructure.db import MIGRATION_LOCK_ID, advisory_lock
from cyberfs.infrastructure.migrate import alembic_config
from cyberfs.infrastructure.settings import get_settings

from .conftest import database_url

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
#
# Alembic is driven exactly as production drives it -- `command.upgrade` against
# the config in `infrastructure/migrate.py`, whose `env.py` reads the URL from
# `Settings`. So the scratch database is selected by overriding `DATABASE_URL`
# and clearing the settings cache, rather than by handing Alembic a different
# URL: that keeps this test on the same code path the container entrypoint uses,
# and needs no synchronous driver, which the project does not install.


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


async def _sql(url: str, statement: str, **params: object) -> list[tuple]:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(statement), params or {})
            return list(result.fetchall()) if result.returns_rows else []
    finally:
        await engine.dispose()


def _tables(url: str) -> set[str]:
    rows = _run(_sql(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    return {row[0] for row in rows}  # type: ignore[index,union-attr]


def _columns(url: str, table: str) -> set[str]:
    rows = _run(
        _sql(
            url, "SELECT column_name FROM information_schema.columns WHERE table_name = :t", t=table
        )
    )
    return {row[0] for row in rows}  # type: ignore[index,union-attr]


@pytest.fixture
def scratch_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A database of its own, with `Settings` pointed at it for the test's duration.

    Not the shared test database: walking it down to base would drop every table
    the rest of the suite depends on.
    """
    admin_url = database_url()
    name = f"cyberfs_rollback_{uuid.uuid4().hex[:10]}"
    scratch_url = admin_url.rsplit("/", 1)[0] + "/" + name

    _run(_sql(admin_url, f'CREATE DATABASE "{name}"'))
    monkeypatch.setenv("DATABASE_URL", scratch_url)
    get_settings.cache_clear()
    try:
        yield scratch_url
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        _run(
            _sql(
                admin_url,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()",
                name=name,
            )
        )
        _run(_sql(admin_url, f'DROP DATABASE IF EXISTS "{name}"'))


def test_every_migration_downgrades_and_upgrades_again(scratch_database: str) -> None:
    """The whole chain, down to base and back up.

    One test rather than one per revision, because a rollback is only meaningful
    as a sequence: a `downgrade` that leaves a stray constraint behind fails on
    the way back up, not where it was written.
    """
    config = alembic_config()

    command.upgrade(config, "head")
    before = _tables(scratch_database)
    assert EXPECTED_TABLES <= before

    command.downgrade(config, "base")

    remaining = _tables(scratch_database) - {"alembic_version"}
    assert remaining == set(), f"downgrade left tables behind: {sorted(remaining)}"

    command.upgrade(config, "head")

    assert _tables(scratch_database) == before, (
        "the schema after down-then-up differs from the schema before"
    )


def test_a_single_step_back_and_forward_keeps_the_newest_column(scratch_database: str) -> None:
    """One step, targeting the most recent migration specifically.

    The full walk above would pass even if the newest `downgrade` were a no-op,
    since a later `upgrade` of an already-present column raises. This checks the
    column actually goes and comes back.
    """
    config = alembic_config()
    command.upgrade(config, "head")
    assert "seal_version_id" in _columns(scratch_database, "file_versions")

    command.downgrade(config, "-1")
    assert "seal_version_id" not in _columns(scratch_database, "file_versions")

    command.upgrade(config, "head")
    assert "seal_version_id" in _columns(scratch_database, "file_versions")


def test_the_backfill_leaves_existing_rows_sealed_under_their_own_id(
    scratch_database: str,
) -> None:
    """The sealing-id migration's central claim, on a row that predates it.

    Content sealed before the column existed was sealed in place, so its sealing
    id is its own id. Seeded at the previous revision -- where the column does not
    exist -- so the row is genuinely older than the migration rather than merely
    written to look that way.
    """
    config = alembic_config()
    command.upgrade(config, "b7e3c9a1d2f5")  # the revision before the sealing id
    assert "seal_version_id" not in _columns(scratch_database, "file_versions")

    node_id, owner_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _run(
        _sql(
            scratch_database,
            "INSERT INTO users (id, subject, root_folder_id, quota_bytes, is_admin, "
            "created_at, updated_at) VALUES (:owner, 'legacy', :node, 0, false, now(), now())",
            owner=owner_id,
            node=node_id,
        )
    )
    _run(
        _sql(
            scratch_database,
            "INSERT INTO nodes (id, owner_id, parent_id, kind, name, normalized_name, revision, "
            "created_at, updated_at, size_bytes, encrypted, encryption_default) VALUES "
            "(:node, :owner, NULL, 'file', 'old.bin', 'old.bin', 1, now(), now(), 10, true, "
            "'inherit')",
            node=node_id,
            owner=owner_id,
        )
    )
    _run(
        _sql(
            scratch_database,
            "INSERT INTO file_versions (id, node_id, owner_id, sequence, size_bytes, "
            "plaintext_digest, content_type, encrypted, created_at, created_by) VALUES "
            "(:vid, :node, :owner, 1, 10, 'deadbeef', 'application/octet-stream', true, now(), "
            "'legacy')",
            vid=version_id,
            node=node_id,
            owner=owner_id,
        )
    )

    command.upgrade(config, "head")

    rows = _run(
        _sql(
            scratch_database,
            "SELECT seal_version_id FROM file_versions WHERE id = :vid",
            vid=version_id,
        )
    )
    assert rows[0][0] == version_id, (  # type: ignore[index]
        "the backfill must leave content written before the column sealed under its own id"
    )
