"""Postgres dump/restore behind the `MetadataDump` port.

Shells out to `pg_dump -Fc` and `pg_restore` via `asyncio.create_subprocess_exec`,
so the application layer never sees a subprocess or a DSN. The custom format is
used because it restores into an empty database in one pass and carries the
schema and data together.

Credentials never reach a log line: the DSN is passed to the child through its
argv/`PGPASSWORD`, and only tool names and revisions are logged. When the binary
is absent the adapter raises `BackupToolUnavailableError`, which is what lets the
integration test skip cleanly on a host without `postgresql-client`.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cyberfs.domain.errors import DependencyUnavailableError
from cyberfs.domain.ports.backup import BinarySink, DumpResult
from cyberfs.infrastructure.logging import get_logger
from cyberfs.infrastructure.migrate import alembic_config

logger = get_logger(__name__)

_CHUNK_BYTES = 1024 * 1024
_HEAD = "head"
PIPE = asyncio.subprocess.PIPE
DEVNULL = asyncio.subprocess.DEVNULL


class BackupToolUnavailableError(DependencyUnavailableError):
    """`pg_dump`/`pg_restore` is not installed on the host."""

    code = "backup_tool_unavailable"
    title = "Backup tooling is unavailable"


async def _tool_version(tool: str) -> str:
    """`<tool> --version`, or a marker when it cannot be determined.

    Best-effort and never raises: this runs on a failure path, where an
    exception would replace a real error with a confusing one.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            tool,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except Exception:
        return "unknown"
    return out.decode(errors="replace").strip() or "unknown"


class MetadataDumpError(DependencyUnavailableError):
    """A dump or restore subprocess exited non-zero."""

    code = "metadata_dump_failed"
    title = "Metadata dump or restore failed"


def libpq_dsn(database_url: str) -> str:
    """Turn a SQLAlchemy async URL into a libpq DSN `pg_dump` understands.

    Strips the `+asyncpg` driver tag; `pg_dump` speaks plain `postgresql://`.
    """
    parts = urlsplit(database_url)
    scheme = parts.scheme.split("+", 1)[0]
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


class PgDumpMetadataDump:
    """Implements the `MetadataDump` port."""

    def __init__(self, engine: AsyncEngine, database_url: str) -> None:
        self._engine = engine
        self._dsn = libpq_dsn(database_url)

    # --- dump ----------------------------------------------------------

    async def dump(self, sink: BinarySink) -> DumpResult:
        revision = await self.current_revision()
        argv = ("pg_dump", "--format=custom", "--no-owner", "--no-privileges", self._dsn)
        proc = await self._spawn(argv, stdout=PIPE)
        hasher = hashlib.sha256()
        size = 0
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(_CHUNK_BYTES):
            hasher.update(chunk)
            size += len(chunk)
            await sink.write(chunk)
        await self._await_exit(proc, "pg_dump")
        logger.info("pg_dump_completed", bytes=size, revision=revision)
        return DumpResult(size=size, checksum=hasher.hexdigest(), schema_revision=revision)

    # --- restore -------------------------------------------------------

    async def restore(
        self, source: AsyncIterator[bytes], *, target_revision: str | None = None
    ) -> tuple[str, ...]:
        argv = ("pg_restore", "--no-owner", "--no-privileges", "--dbname", self._dsn)
        proc = await self._spawn(argv, stdin=PIPE, stdout=DEVNULL)
        assert proc.stdin is not None
        async for chunk in source:
            proc.stdin.write(chunk)
            await proc.stdin.drain()
        proc.stdin.close()
        await self._await_exit(proc, "pg_restore")

        loaded = await self.current_revision()
        target = target_revision or _HEAD
        await asyncio.to_thread(command.upgrade, alembic_config(), target)
        path = self._upgrade_path(loaded, await self.current_revision())
        logger.info("pg_restore_completed", loaded=loaded, migrations=len(path))
        return path

    # --- revision ------------------------------------------------------

    async def current_revision(self) -> str:
        async with self._engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            value = result.scalar_one_or_none()
        return str(value) if value is not None else ""

    async def referenced_object_keys(self) -> frozenset[str]:
        stmt = text("SELECT owner_id, node_id, id FROM file_versions")
        async with self._engine.connect() as connection:
            rows = (await connection.execute(stmt)).all()
        return frozenset(f"{owner}/{node}/{version}" for owner, node, version in rows)

    # --- helpers -------------------------------------------------------

    @staticmethod
    def _upgrade_path(from_revision: str, to_revision: str) -> tuple[str, ...]:
        """The revisions applied to carry `from_revision` up to `to_revision`."""
        if not to_revision or from_revision == to_revision:
            return ()
        script = ScriptDirectory.from_config(alembic_config())
        lower = from_revision or None
        applied = [rev.revision for rev in script.iterate_revisions(to_revision, lower)]
        return tuple(reversed(applied))

    @staticmethod
    async def _spawn(
        argv: tuple[str, ...], *, stdin: int | None = None, stdout: int | None = None
    ) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *argv, stdin=stdin, stdout=stdout, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError as exc:
            raise BackupToolUnavailableError(f"{argv[0]} is not installed") from exc

    @staticmethod
    async def _await_exit(proc: asyncio.subprocess.Process, tool: str) -> None:
        if proc.stderr is not None:
            # Drain the pipe so `wait` cannot deadlock; the content is
            # deliberately discarded -- it can echo connection parameters.
            await proc.stderr.read()
        await proc.wait()
        if proc.returncode != 0:
            # Never the DSN or stderr, which can echo connection parameters.
            # The tool's own version is safe and is the single most useful fact
            # here: `pg_dump` refuses a server newer than itself, and that
            # mismatch is otherwise indistinguishable from any other non-zero
            # exit once stderr has been dropped.
            logger.error(
                "metadata_tool_failed",
                tool=tool,
                returncode=proc.returncode,
                tool_version=await _tool_version(tool),
            )
            raise MetadataDumpError(f"{tool} exited with {proc.returncode}")
