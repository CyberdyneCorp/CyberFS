"""Unit tests for the pg_dump/pg_restore adapter.

These exercise the parts that do not need a live Postgres or the binaries: DSN
conversion, the upgrade-path walk over the real Alembic history, absent-binary
handling, and the guarantee that a failure never carries the DSN.
"""

from __future__ import annotations

import asyncio

import pytest

from cyberfs.adapters.outbound.backup import pg_dump
from cyberfs.adapters.outbound.backup.pg_dump import (
    BackupToolUnavailableError,
    MetadataDumpError,
    PgDumpMetadataDump,
    libpq_dsn,
)

DSN = "postgresql+asyncpg://cyberfs:s3cr3t@db:5432/cyberfs"


def test_libpq_dsn_strips_the_async_driver() -> None:
    assert libpq_dsn(DSN) == "postgresql://cyberfs:s3cr3t@db:5432/cyberfs"


def test_libpq_dsn_leaves_a_plain_url_untouched() -> None:
    plain = "postgresql://u:p@host/db"
    assert libpq_dsn(plain) == plain


def test_upgrade_path_walks_the_real_history() -> None:
    path = PgDumpMetadataDump._upgrade_path("079de84e4f44", "b1f7c0a2d3e4")
    assert path == ("b1f7c0a2d3e4",)


def test_upgrade_path_is_empty_when_already_at_target() -> None:
    assert PgDumpMetadataDump._upgrade_path("b1f7c0a2d3e4", "b1f7c0a2d3e4") == ()


def test_upgrade_path_is_empty_without_a_target() -> None:
    assert PgDumpMetadataDump._upgrade_path("079de84e4f44", "") == ()


@pytest.mark.asyncio
async def test_spawn_raises_typed_error_when_binary_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _missing(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("pg_dump")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _missing)
    with pytest.raises(BackupToolUnavailableError):
        await PgDumpMetadataDump._spawn(("pg_dump",), stdout=asyncio.subprocess.PIPE)


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes) -> None:
        self.returncode = returncode
        self.stderr = _FakeStream(stderr)

    async def wait(self) -> None:
        return None


@pytest.mark.asyncio
async def test_await_exit_failure_omits_the_dsn() -> None:
    # stderr deliberately echoes something DSN-shaped; it must never surface.
    proc = _FakeProc(returncode=1, stderr=b"connection to postgresql://u:s3cr3t@db failed")
    with pytest.raises(MetadataDumpError) as excinfo:
        await PgDumpMetadataDump._await_exit(proc, "pg_dump")  # type: ignore[arg-type]

    message = str(excinfo.value)
    assert "s3cr3t" not in message
    assert "pg_dump" in message


@pytest.mark.asyncio
async def test_await_exit_success_is_silent() -> None:
    proc = _FakeProc(returncode=0, stderr=b"")
    await PgDumpMetadataDump._await_exit(proc, "pg_restore")  # type: ignore[arg-type]


def test_adapter_module_never_logs_the_dsn_string(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard: constructing the adapter keeps the DSN off any log call.
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(pg_dump.logger, "info", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(pg_dump.logger, "error", lambda *a, **k: calls.append((a, k)))
    PgDumpMetadataDump(engine=object(), database_url=DSN)  # type: ignore[arg-type]
    assert calls == []
