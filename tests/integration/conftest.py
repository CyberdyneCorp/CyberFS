"""Integration fixtures: a real Postgres, migrated, truncated between tests.

Skipped entirely when no database is reachable, so `just test` stays usable
without Docker running -- but set `CYBERFS_REQUIRE_INTEGRATION` and an
unreachable dependency becomes a failure instead. CI sets it: a suite that
silently skips is worse than no suite, because it reports success either way.

Connection details come from the environment, falling back to the ports in
`docker-compose.yml`. They are read from `CYBERFS_TEST_*` first and the
application's own variables second, so a runner that has already configured the
app (as CI does) does not have to configure the tests separately.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cyberfs.adapters.outbound.db.models import Base
from cyberfs.adapters.outbound.db.unit_of_work import SqlUnitOfWork
from cyberfs.infrastructure.db import create_engine, create_session_factory
from cyberfs.infrastructure.settings import Environment, Settings

DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://cyberfs:cyberfs@localhost:5434/cyberfs"
DEFAULT_TEST_REDIS_URL = "redis://localhost:6380/0"
DEFAULT_TEST_MINIO_ENDPOINT = "localhost:9000"
TEST_MASTER_KEY = base64.b64encode(b"\x07" * 32).decode()


def _setting(*names: str, default: str) -> str:
    """First environment variable that is set, else the compose default."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def database_url() -> str:
    return _setting("CYBERFS_TEST_DATABASE_URL", "DATABASE_URL", default=DEFAULT_TEST_DATABASE_URL)


def redis_url() -> str:
    return _setting("CYBERFS_TEST_REDIS_URL", "REDIS_URL", default=DEFAULT_TEST_REDIS_URL)


def unreachable(reason: str) -> None:
    """Skip, unless the runner has declared that these tests must run.

    Without the guard a missing dependency silently turns the whole suite green,
    which is how 205 of these tests skipped in CI for months while the job
    reported success.
    """
    if os.environ.get("CYBERFS_REQUIRE_INTEGRATION"):
        pytest.fail(f"{reason} -- CYBERFS_REQUIRE_INTEGRATION is set, refusing to skip")
    pytest.skip(reason)


def build_settings(**overrides: object) -> Settings:
    """Construct settings through the constructor so validation runs.

    `model_copy(update=...)` skips validation, which silently leaves
    `SecretStr` fields as plain strings.
    """
    base: dict[str, object] = {
        "environment": Environment.TEST,
        "database_url": database_url(),
        "redis_url": redis_url(),
        "minio_endpoint": _setting(
            "CYBERFS_TEST_MINIO_ENDPOINT", "MINIO_ENDPOINT", default=DEFAULT_TEST_MINIO_ENDPOINT
        ),
        # Must match whatever MinIO the runner started, or every object-store
        # test fails on authentication rather than on the behaviour under test.
        "minio_access_key": _setting(
            "CYBERFS_TEST_MINIO_ACCESS_KEY", "MINIO_ACCESS_KEY", default="cyberfs"
        ),
        "minio_secret_key": _setting(
            "CYBERFS_TEST_MINIO_SECRET_KEY", "MINIO_SECRET_KEY", default="cyberfs-dev-secret"
        ),
        "minio_bucket": "cyberfs-content",
        "minio_secure": False,
        "cyberdyne_auth_base_url": "https://auth.example.test",
        "cyberfs_client_id": "cyberfs",
        "cyberfs_client_secret": "integration-client-value",
        "master_key": TEST_MASTER_KEY,
        "_env_file": None,
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


#: Remembers a failed connection so a run without Docker skips instantly
#: instead of retrying the handshake once per test.
_unreachable: str | None = None


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test.

    Function-scoped deliberately: asyncpg connections are bound to the event
    loop that created them, and pytest-asyncio gives each test its own loop, so
    a session-scoped engine would be reused across loops and fail.
    """
    global _unreachable
    if _unreachable is not None:
        unreachable(_unreachable)

    engine = create_engine(build_settings())
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        _unreachable = f"no Postgres at {database_url()}: {type(exc).__name__}"
        unreachable(_unreachable)

    # Create the schema directly rather than via Alembic: the migration itself
    # is exercised by `test_migrations.py`, and this keeps the fixture fast.
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE users, nodes, file_versions, grants, public_links, "
                "user_keys, wrapped_data_keys, quota_usage, audit_records "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield create_session_factory(engine)


@pytest.fixture
async def uow(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[SqlUnitOfWork]:
    async with SqlUnitOfWork(session_factory) as unit:
        yield unit
