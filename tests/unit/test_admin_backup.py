"""The admin backup surface: audit on trigger and the operations summary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.adapters.inbound.api.schemas import BackupRecordSummary, BackupSummary
from cyberfs.application.admin import AdminService
from cyberfs.domain.audit import AuditAction
from cyberfs.domain.backup import BackupManifest, BackupRecord, ManifestEntry, SkewReport
from tests.unit.fakes import FakeUnitOfWork

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _verified(finished_at: datetime, *, total_bytes: int = 2048) -> BackupRecord:
    manifest = BackupManifest.from_entries(
        (ManifestEntry(key="o/n/v", size=total_bytes, checksum="abc"),),
        dump_checksum="dump",
        dump_size=10,
        schema_revision="079de84e4f44",
    )
    started = BackupRecord.start(
        started_at=finished_at - timedelta(seconds=5), schema_revision="079de84e4f44"
    )
    return started.mark_verified(finished_at=finished_at, manifest=manifest, skew=SkewReport())


def _failed(finished_at: datetime) -> BackupRecord:
    started = BackupRecord.start(started_at=finished_at - timedelta(seconds=1), schema_revision="r")
    return started.mark_failed(finished_at=finished_at, error="MetadataDumpError")


# --- audit -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_backup_triggered_writes_an_audit_record() -> None:
    uow = FakeUnitOfWork()
    await AdminService().note_backup_triggered(uow, "admin-1")
    assert len(uow.audit.records) == 1
    record = uow.audit.records[0]
    assert record.action is AuditAction.BACKUP_TRIGGERED
    assert record.actor_subject == "admin-1"


# --- operations summary ----------------------------------------------------


def test_summary_when_disabled_is_not_stale() -> None:
    summary = BackupSummary.of((), enabled=False, max_age_hours=48, now=NOW)
    assert summary.enabled is False
    assert summary.stale is False
    assert summary.last_backup_at is None


def test_summary_with_no_history_when_enabled_is_stale() -> None:
    summary = BackupSummary.of((), enabled=True, max_age_hours=48, now=NOW)
    assert summary.stale is True


def test_summary_reports_the_latest_verified_backup() -> None:
    record = _verified(NOW - timedelta(hours=1), total_bytes=4096)
    summary = BackupSummary.of((record,), enabled=True, max_age_hours=48, now=NOW)
    assert summary.stale is False
    assert summary.last_verified is True
    assert summary.last_size_bytes == 4096
    assert summary.last_verified_at == record.finished_at
    assert summary.schema_revision == "079de84e4f44"


def test_summary_is_stale_past_the_freshness_window() -> None:
    record = _verified(NOW - timedelta(hours=72))
    summary = BackupSummary.of((record,), enabled=True, max_age_hours=48, now=NOW)
    assert summary.stale is True


def test_summary_stale_when_latest_run_failed_after_an_old_success() -> None:
    """A recent failure with a too-old last success still reads as stale."""
    records = (_failed(NOW), _verified(NOW - timedelta(hours=72)))
    summary = BackupSummary.of(records, enabled=True, max_age_hours=48, now=NOW)
    assert summary.last_outcome == "failed"
    assert summary.last_verified is False
    assert summary.stale is True


# --- record summary --------------------------------------------------------


def test_record_summary_carries_no_object_key_or_secret() -> None:
    record = _verified(NOW)
    summary = BackupRecordSummary.of(record)
    assert summary.verified is True
    assert summary.has_skew is False
    fields = set(BackupRecordSummary.model_fields)
    assert not fields & {"wrapped_dek", "wrapped_kek", "dek", "kek", "master_key"}


def test_record_summary_of_a_uuid_backup_round_trips_the_state() -> None:
    record = _failed(NOW)
    summary = BackupRecordSummary.of(record)
    assert isinstance(summary.id, uuid.UUID)
    assert summary.state == "failed"
    assert summary.error == "MetadataDumpError"
