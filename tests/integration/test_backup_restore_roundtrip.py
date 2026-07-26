"""A real backup and a real restore, end to end (task 11.14).

`backup-restore/spec.md`, "Restore is tested automatically": seed a tree with
encrypted and unencrypted files, multiple versions, a share, a public link, and
a trashed node; run a real `BackupService.run` against live Postgres and MinIO;
restore into a scratch database and bucket via `RestoreService`; and assert
byte-level fidelity of every file plus that no node, version, grant, or public
link went missing.

Skips gracefully -- exactly like `conftest.py` skips when Postgres is
unreachable -- when MinIO is down or when `pg_dump`/`pg_restore` are not
installed, so the suite stays runnable on a workstation without the tooling.
"""

from __future__ import annotations

import shutil
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from alembic.script import ScriptDirectory
from minio import Minio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.adapters.inbound.api.composition import build_backup_object_store
from cyberfs.adapters.outbound.backup.pg_dump import PgDumpMetadataDump
from cyberfs.adapters.outbound.db.backup_repository import SqlBackupRepository
from cyberfs.adapters.outbound.objects.minio_store import MinioObjectStore
from cyberfs.application.restore import RestoreService
from cyberfs.infrastructure.db import create_engine, ping
from cyberfs.infrastructure.migrate import alembic_config
from cyberfs.infrastructure.settings import Environment, Settings
from cyberfs.restore import _key_available, _readiness, _target_is_empty

from .conftest import (
    build_settings,
    database_url,
    minio_access_key,
    minio_endpoint,
    minio_secret_key,
)

pytestmark = pytest.mark.integration

ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}

ENDPOINT = minio_endpoint()
ACCESS_KEY = minio_access_key()
SECRET_KEY = minio_secret_key()

PLAIN_V1 = b"first revision of the plain file\n" * 40
PLAIN_V2 = b"second revision, replacing the first\n" * 40
SECRET = b"QUARTERLY-REVENUE-CONFIDENTIAL\n" * 40
LINKED = b"anyone with the link may read this\n" * 40
TRASHED = b"this file is deleted before the backup\n" * 40


def _minio() -> Minio:
    return Minio(ENDPOINT, access_key=ACCESS_KEY, secret_key=SECRET_KEY, secure=False)


def _skip_unless_ready() -> None:
    if not (shutil.which("pg_dump") and shutil.which("pg_restore")):
        pytest.skip("pg_dump/pg_restore not installed")
    try:
        _minio().list_buckets()
    except Exception as exc:
        pytest.skip(f"no MinIO at {ENDPOINT}: {type(exc).__name__}")


def _ensure_bucket(name: str) -> None:
    client = _minio()
    if not client.bucket_exists(name):
        client.make_bucket(name)


def _remove_bucket(name: str) -> None:
    client = _minio()
    if not client.bucket_exists(name):
        return
    for entry in client.list_objects(name, recursive=True):
        if entry.object_name is not None:
            client.remove_object(name, entry.object_name)
    client.remove_bucket(name)


def _scratch_url(name: str) -> str:
    parts = urlsplit(database_url())
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


async def _ensure_alembic_version(engine: AsyncEngine) -> None:
    """The conftest schema is built with `create_all`, which does not stamp.

    `pg_dump`/`current_revision` need an `alembic_version` row, so seed the
    deployed head if the table is empty. Harmless when migrations already ran.
    """
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) NOT NULL)")
        )
        existing = (await conn.execute(text("SELECT version_num FROM alembic_version"))).first()
        if existing is None and head is not None:
            await conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head}
            )


async def _client(app: object) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _root(http: httpx.AsyncClient, who: dict[str, str]) -> str:
    response = await http.get("/api/v1/nodes/root", headers=who)
    return str(response.json()["id"])


async def _upload(
    http: httpx.AsyncClient, parent: str, name: str, body: bytes, *, encrypted: bool = False
) -> str:
    suffix = "?encrypted=true" if encrypted else ""
    response = await http.put(
        f"/api/v1/nodes/{parent}/files/{name}{suffix}", content=body, headers=ALICE
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _seed(http: httpx.AsyncClient) -> dict[str, str]:
    """Seed the tree and return the node ids we assert against later."""
    await _root(http, BOB)  # provision Bob so a share can resolve him
    root = await _root(http, ALICE)

    plain = await _upload(http, root, "plain.txt", PLAIN_V1)
    replaced = await http.put(f"/api/v1/nodes/{plain}/content", content=PLAIN_V2, headers=ALICE)
    assert replaced.status_code == 200, replaced.text

    secret = await _upload(http, root, "secret.bin", SECRET, encrypted=True)
    granted = await http.put(
        f"/api/v1/nodes/{secret}/grants", json={"recipient": "bob", "role": "viewer"}, headers=ALICE
    )
    assert granted.status_code == 201, granted.text

    linked = await _upload(http, root, "linked.txt", LINKED)
    issued = await http.post(f"/api/v1/nodes/{linked}/links", json={}, headers=ALICE)
    assert issued.status_code == 201, issued.text

    trashed = await _upload(http, root, "trashed.bin", TRASHED)
    deleted = await http.delete(f"/api/v1/nodes/{trashed}", headers=ALICE)
    assert deleted.status_code in (200, 204), deleted.text

    return {"plain": plain, "secret": secret, "linked": linked, "trashed": trashed}


async def _row_counts(engine: AsyncEngine) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with engine.connect() as conn:
        for table in ("nodes", "file_versions", "grants", "public_links"):
            result = await conn.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            counts[table] = int(result.scalar_one())
    return counts


def _restore_service(
    scratch_engine: AsyncEngine,
    scratch_settings: Settings,
    source_app: object,
    backup_settings: Settings,
) -> RestoreService:
    target = MinioObjectStore(_minio(), scratch_settings.minio_bucket)
    return RestoreService(
        metadata_dump=PgDumpMetadataDump(scratch_engine, scratch_settings.database_url),
        backup_store=build_backup_object_store(backup_settings),
        target_store=target,
        repository=SqlBackupRepository(source_app.state.session_factory),  # type: ignore[attr-defined]
        readiness=lambda: _readiness(scratch_engine, target),
        key_available=lambda: _key_available(scratch_engine, scratch_settings),
        target_is_empty=lambda: _target_is_empty(scratch_engine),
    )


async def _drop_database(maint: AsyncEngine, name: str) -> None:
    async with maint.connect() as conn:
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": name},
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


async def _download(http: httpx.AsyncClient, node_id: str, who: dict[str, str]) -> httpx.Response:
    return await http.get(f"/api/v1/nodes/{node_id}/content", headers=who)


async def _skip_if_db_down() -> None:
    engine = create_engine(build_settings())
    try:
        await ping(engine)
    except Exception as exc:
        pytest.skip(f"no Postgres at {database_url()}: {type(exc).__name__}")
    finally:
        await engine.dispose()


async def test_backup_restore_roundtrip(session_factory: object) -> None:
    # `session_factory` (via the `engine` fixture) guarantees a clean,
    # schema-present source database, exactly as the sibling suites rely on.
    _skip_unless_ready()
    await _skip_if_db_down()

    tag = uuid.uuid4().hex[:10]
    source_bucket = f"cyberfs-src-{tag}"
    backup_bucket = f"cyberfs-bak-{tag}"
    scratch_bucket = f"cyberfs-dst-{tag}"
    scratch_db = f"cyberfs_restore_{tag}"

    source_settings = build_settings(
        auth_dev_mode=True,
        environment=Environment.TEST,
        minio_endpoint=ENDPOINT,
        minio_access_key=ACCESS_KEY,
        minio_secret_key=SECRET_KEY,
        minio_bucket=source_bucket,
        minio_secure=False,
        backup_enabled=True,
        backup_cron="0 3 * * *",
        backup_s3_endpoint=ENDPOINT,
        backup_s3_access_key=ACCESS_KEY,
        backup_s3_secret_key=SECRET_KEY,
        backup_s3_bucket=backup_bucket,
        backup_verify_sample_count=5,
    )
    scratch_settings = build_settings(
        auth_dev_mode=True,
        environment=Environment.TEST,
        database_url=_scratch_url(scratch_db),
        minio_endpoint=ENDPOINT,
        minio_access_key=ACCESS_KEY,
        minio_secret_key=SECRET_KEY,
        minio_bucket=scratch_bucket,
        minio_secure=False,
    )

    for bucket in (source_bucket, backup_bucket, scratch_bucket):
        _ensure_bucket(bucket)

    source_app = create_app(source_settings)
    verify_app = create_app(scratch_settings)
    maint = create_async_engine(database_url(), isolation_level="AUTOCOMMIT")
    scratch_engine = create_engine(scratch_settings)
    engines: list[AsyncEngine] = [maint, scratch_engine]

    try:
        await _ensure_alembic_version(source_app.state.engine)
        async with await _client(source_app) as http:
            ids = await _seed(http)
        source_counts = await _row_counts(source_app.state.engine)

        record = await source_app.state.backup.run()
        assert record.is_verified, f"backup did not verify: {record.error}"

        async with maint.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))

        service = _restore_service(scratch_engine, scratch_settings, source_app, source_settings)
        report = await service.restore(record.id, destructive=False)
        assert report.healthy, "restored stack did not report healthy"
        assert report.key_available
        assert report.objects_restored == record.object_count

        await _assert_fidelity(verify_app, ids)
        scratch_counts = await _row_counts(scratch_engine)
        assert scratch_counts == source_counts, "a node, version, grant, or link went missing"
    finally:
        await source_app.state.engine.dispose()
        await verify_app.state.engine.dispose()
        await scratch_engine.dispose()
        engines.remove(scratch_engine)
        await _drop_database(maint, scratch_db)
        for engine in engines:
            await engine.dispose()
        for bucket in (source_bucket, backup_bucket, scratch_bucket):
            _remove_bucket(bucket)


async def _assert_fidelity(verify_app: object, ids: dict[str, str]) -> None:
    """Every file's restored bytes match the original, encrypted ones decrypt."""
    async with await _client(verify_app) as http:
        plain = await _download(http, ids["plain"], ALICE)
        assert plain.status_code == 200
        assert plain.content == PLAIN_V2, "current version bytes differ after restore"

        # Two versions survived the round trip.
        versions = await http.get(f"/api/v1/nodes/{ids['plain']}/versions", headers=ALICE)
        assert len(versions.json()["items"]) == 2

        # Encrypted content decrypts for the owner with the same MASTER_KEY.
        owner_secret = await _download(http, ids["secret"], ALICE)
        assert owner_secret.status_code == 200
        assert owner_secret.content == SECRET

        # And for the share recipient.
        recipient_secret = await _download(http, ids["secret"], BOB)
        assert recipient_secret.status_code == 200
        assert recipient_secret.content == SECRET

        linked = await _download(http, ids["linked"], ALICE)
        assert linked.content == LINKED

        # The trashed node still exists as a row but serves no content.
        trashed = await _download(http, ids["trashed"], ALICE)
        assert trashed.status_code == 404
