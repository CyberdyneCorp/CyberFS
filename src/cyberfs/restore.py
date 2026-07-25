"""Scripted restore entrypoint: `python -m cyberfs.restore --backup-id <id>`.

Rebuilds a stack from a named backup. Non-destructive by default -- a target
that already holds data is refused unless `--destructive` is given. On success
it prints the migration upgrade path taken and whether the restored service is
healthy; a stack whose `MASTER_KEY` cannot open the restored key material is
reported not healthy and the command exits non-zero.

The wiring lives here rather than in the API composition root because a restore
runs standalone, often against a scratch stack, without the API process.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass

from minio import Minio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cyberfs.adapters.outbound.backup.pg_dump import PgDumpMetadataDump
from cyberfs.adapters.outbound.crypto import MasterKeyProvider
from cyberfs.adapters.outbound.db.backup_repository import SqlBackupRepository
from cyberfs.adapters.outbound.objects.minio_store import MinioObjectStore
from cyberfs.application.restore import RestoreReport, RestoreService
from cyberfs.domain.errors import KeyUnavailableError
from cyberfs.infrastructure.db import create_engine, create_session_factory, ping
from cyberfs.infrastructure.logging import configure_logging, get_logger
from cyberfs.infrastructure.settings import Settings, get_settings

logger = get_logger(__name__)


def _minio(endpoint: str, access_key: str, secret_key: str, *, secure: bool) -> Minio:
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)


def _backup_store(settings: Settings) -> MinioObjectStore:
    if (
        settings.backup_s3_endpoint is None
        or settings.backup_s3_access_key is None
        or settings.backup_s3_secret_key is None
        or settings.backup_s3_bucket is None
    ):
        raise ValueError("BACKUP_S3_* must be configured to restore")
    client = _minio(
        settings.backup_s3_endpoint,
        settings.backup_s3_access_key.get_secret_value(),
        settings.backup_s3_secret_key.get_secret_value(),
        secure=settings.minio_secure,
    )
    return MinioObjectStore(client, settings.backup_s3_bucket)


def _target_store(settings: Settings) -> MinioObjectStore:
    client = _minio(
        settings.minio_endpoint,
        settings.minio_access_key.get_secret_value(),
        settings.minio_secret_key.get_secret_value(),
        secure=settings.minio_secure,
    )
    return MinioObjectStore(client, settings.minio_bucket)


async def _target_is_empty(engine: AsyncEngine) -> bool:
    """Whether the target database holds no application rows.

    A missing `nodes` table -- a genuinely empty stack whose schema the restore
    will recreate -- counts as empty rather than an error.
    """
    try:
        async with engine.connect() as connection:
            found = await connection.execute(text("SELECT 1 FROM nodes LIMIT 1"))
            return found.scalar_one_or_none() is None
    except Exception:  # table absent: an un-migrated, empty stack
        return True


async def _key_available(engine: AsyncEngine, settings: Settings) -> bool:
    """Whether `MASTER_KEY` opens a restored user's sealed KEK.

    A stack with no user keys is trivially fine. A wrapped KEK that fails to
    unseal means the deployment key does not match the restored material, so
    encrypted content is unreadable and the service is not healthy.
    """
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT wrapped_kek, master_key_id FROM user_keys LIMIT 1")
        )
        row = result.first()
    if row is None:
        return True
    provider = MasterKeyProvider(
        settings.master_key_bytes, previous=settings.master_key_previous_bytes
    )
    try:
        provider.unwrap_kek(bytes(row[0]), master_key_id=row[1])
    except KeyUnavailableError:
        return False
    return True


async def _readiness(engine: AsyncEngine, target: MinioObjectStore) -> bool:
    try:
        await ping(engine)
        await target.stat("__healthcheck__")
    except Exception:
        return False
    return True


@dataclass(frozen=True, slots=True)
class _Wiring:
    service: RestoreService
    engine: AsyncEngine


def _build(settings: Settings) -> _Wiring:
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    target = _target_store(settings)
    service = RestoreService(
        metadata_dump=PgDumpMetadataDump(engine, settings.database_url),
        backup_store=_backup_store(settings),
        target_store=target,
        repository=SqlBackupRepository(sessions),
        readiness=lambda: _readiness(engine, target),
        key_available=lambda: _key_available(engine, settings),
        target_is_empty=lambda: _target_is_empty(engine),
    )
    return _Wiring(service=service, engine=engine)


def _print_report(report: RestoreReport) -> None:
    path = " -> ".join(report.migrations_applied) or "(none)"
    print(f"backup:      {report.backup_id}")
    print(f"schema:      {report.schema_revision}")
    print(f"migrations:  {path}")
    print(f"objects:     {report.objects_restored}")
    print(f"key present: {report.key_available}")
    print(f"healthy:     {report.healthy}")


async def _run(backup_id: uuid.UUID, *, destructive: bool) -> int:
    settings = get_settings()
    wiring = _build(settings)
    try:
        report = await wiring.service.restore(backup_id, destructive=destructive)
    finally:
        await wiring.engine.dispose()
    _print_report(report)
    return 0 if report.healthy else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cyberfs.restore", description="Restore a CyberFS backup."
    )
    parser.add_argument("--backup-id", required=True, type=uuid.UUID, help="backup identifier")
    parser.add_argument(
        "--destructive",
        action="store_true",
        help="overwrite a target that already holds data",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(get_settings().log_level)
    try:
        return asyncio.run(_run(args.backup_id, destructive=args.destructive))
    except Exception as exc:
        logger.error("restore_failed", error=type(exc).__name__, detail=str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
