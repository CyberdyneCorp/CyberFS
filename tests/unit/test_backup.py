"""Backup domain value objects and pure policy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from cyberfs.domain.backup import (
    BackupManifest,
    BackupRecord,
    BackupState,
    ManifestEntry,
    RetentionDecision,
    SkewReport,
    decide_retention,
    detect_skew,
    is_stale,
)


def _entry(key: str, size: int = 10) -> ManifestEntry:
    return ManifestEntry(key=key, size=size, checksum=f"sum-{key}")


def _manifest(keys: tuple[str, ...] = ("a", "b", "c")) -> BackupManifest:
    return BackupManifest.from_entries(
        tuple(_entry(k) for k in keys),
        dump_checksum="dump-sum",
        dump_size=1234,
        schema_revision="079de84e4f44",
    )


def _at(hours_ago: float, *, base: datetime | None = None) -> datetime:
    base = base or datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    return base - timedelta(hours=hours_ago)


def _record(
    *,
    state: BackupState = BackupState.VERIFIED,
    finished_at: datetime | None,
    started_at: datetime | None = None,
    backup_id: uuid.UUID | None = None,
) -> BackupRecord:
    started = started_at or (finished_at - timedelta(minutes=5) if finished_at else _at(0))
    return BackupRecord(
        id=backup_id or uuid.uuid4(),
        started_at=started,
        state=state,
        schema_revision="079de84e4f44",
        finished_at=finished_at,
    )


# --- ManifestEntry / BackupManifest ---------------------------------------


def test_from_entries_derives_totals() -> None:
    manifest = _manifest(("a", "b", "c"))
    assert manifest.object_count == 3
    assert manifest.total_bytes == 30


def test_manifest_json_round_trip() -> None:
    manifest = _manifest(("x", "y"))
    restored = BackupManifest.from_json(manifest.to_json())
    assert restored == manifest


def test_manifest_json_is_deterministic() -> None:
    assert _manifest().to_json() == _manifest().to_json()


def test_manifest_json_carries_only_keys_sizes_checksums() -> None:
    """No secret can hide in a manifest -- the schema has no field for one."""
    payload = _manifest(("secret-looking-key",)).to_json()
    for forbidden in ("master_key", "MASTER_KEY", "kek", "dek", "plaintext"):
        assert forbidden not in payload


def test_sample_is_deterministic_for_a_seed() -> None:
    manifest = _manifest(tuple(str(i) for i in range(20)))
    first = manifest.sample(5, seed="run-1")
    second = manifest.sample(5, seed="run-1")
    assert first == second
    assert len(first) == 5


def test_sample_seed_changes_selection() -> None:
    manifest = _manifest(tuple(str(i) for i in range(50)))
    assert manifest.sample(5, seed="a") != manifest.sample(5, seed="b")


def test_sample_count_at_or_above_size_returns_all() -> None:
    manifest = _manifest(("a", "b"))
    assert manifest.sample(2, seed="s") == manifest.entries
    assert manifest.sample(99, seed="s") == manifest.entries


def test_sample_zero_or_negative_is_empty() -> None:
    manifest = _manifest(("a", "b"))
    assert manifest.sample(0, seed="s") == ()
    assert manifest.sample(-3, seed="s") == ()


# --- BackupRecord ----------------------------------------------------------


def test_start_creates_a_running_record() -> None:
    record = BackupRecord.start(started_at=_at(0), schema_revision="rev")
    assert record.state is BackupState.RUNNING
    assert not record.is_verified
    assert record.finished_at is None
    assert record.duration_seconds is None


def test_bucket_keys_derive_from_id() -> None:
    backup_id = uuid.uuid4()
    record = BackupRecord.start(started_at=_at(0), schema_revision="rev", backup_id=backup_id)
    assert record.bucket_prefix == f"backups/{backup_id}"
    assert record.dump_key == f"backups/{backup_id}/dump.sql.gz"
    assert record.manifest_key == f"backups/{backup_id}/manifest.json"


def test_mark_verified_records_manifest_and_skew() -> None:
    started = _at(1)
    record = BackupRecord.start(started_at=started, schema_revision="rev")
    manifest = _manifest(("a", "b"))
    skew = SkewReport(missing_in_dump=("a",), missing_in_manifest=())
    verified = record.mark_verified(
        finished_at=started + timedelta(minutes=3), manifest=manifest, skew=skew
    )

    assert verified.is_verified
    assert verified.dump_checksum == "dump-sum"
    assert verified.object_count == 2
    assert verified.total_bytes == 20
    assert verified.skew_missing_in_dump == 1
    assert verified.skew_missing_in_manifest == 0
    assert verified.has_skew
    assert verified.duration_seconds == 180.0
    # The original is untouched -- records are immutable.
    assert record.state is BackupState.RUNNING


def test_mark_failed_records_the_error() -> None:
    record = BackupRecord.start(started_at=_at(1), schema_revision="rev")
    failed = record.mark_failed(finished_at=_at(0), error="checksum mismatch")
    assert failed.is_failed
    assert failed.error == "checksum mismatch"
    assert not failed.has_skew


# --- detect_skew -----------------------------------------------------------


def test_detect_skew_none_when_sets_agree() -> None:
    keys = frozenset({"a", "b"})
    report = detect_skew(keys, keys)
    assert not report.has_skew
    assert report == SkewReport()


def test_detect_skew_missing_in_dump() -> None:
    report = detect_skew(frozenset({"a", "b"}), frozenset({"a"}))
    assert report.missing_in_dump == ("b",)
    assert report.missing_in_manifest == ()
    assert report.has_skew


def test_detect_skew_missing_in_manifest() -> None:
    report = detect_skew(frozenset({"a"}), frozenset({"a", "z"}))
    assert report.missing_in_manifest == ("z",)
    assert report.has_skew


def test_detect_skew_tolerates_in_flight_window() -> None:
    report = detect_skew(
        frozenset({"a", "b"}),
        frozenset({"a"}),
        in_flight=frozenset({"b"}),
    )
    assert not report.has_skew


def test_detect_skew_results_are_sorted() -> None:
    report = detect_skew(frozenset({"c", "a", "b"}), frozenset())
    assert report.missing_in_dump == ("a", "b", "c")


# --- decide_retention ------------------------------------------------------


def _policy(records: tuple[BackupRecord, ...], **overrides: object) -> RetentionDecision:
    params: dict[str, object] = {
        "keep_daily": 2,
        "keep_weekly": 0,
        "keep_monthly": 0,
        "now": datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        "failed_grace_hours": 24,
    }
    params.update(overrides)
    return decide_retention(records, **params)  # type: ignore[arg-type]


def test_retention_keeps_newest_per_day() -> None:
    # Three days, two verified per day. keep_daily=2 keeps the newest of each of
    # the two most recent days.
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    day0_new = _record(finished_at=now - timedelta(hours=1))
    day0_old = _record(finished_at=now - timedelta(hours=5))
    day1 = _record(finished_at=now - timedelta(days=1))
    day2 = _record(finished_at=now - timedelta(days=2))
    decision = _policy((day0_new, day0_old, day1, day2), now=now)

    assert set(decision.keep) == {day0_new.id, day1.id}
    assert set(decision.delete) == {day0_old.id, day2.id}
    assert not decision.protected_last_verified


def test_retention_protects_the_only_verified_backup() -> None:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    # keep_daily=0 would drop everything -- but the last verified is protected.
    only = _record(finished_at=now - timedelta(days=40))
    decision = _policy((only,), keep_daily=0, now=now)

    assert decision.keep == (only.id,)
    assert decision.delete == ()
    assert decision.protected_last_verified


def test_retention_weekly_and_monthly_buckets() -> None:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    this_week = _record(finished_at=now - timedelta(days=1))
    last_week = _record(finished_at=now - timedelta(days=8))
    last_month = _record(finished_at=now - timedelta(days=40))
    decision = _policy(
        (this_week, last_week, last_month),
        keep_daily=0,
        keep_weekly=2,
        keep_monthly=2,
        now=now,
    )
    assert this_week.id in decision.keep
    assert last_week.id in decision.keep
    # last_month is the newest of the second-most-recent month (June).
    assert last_month.id in decision.keep
    assert decision.delete == ()


def test_retention_prunes_failed_past_grace() -> None:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    keeper = _record(finished_at=now - timedelta(hours=1))
    old_failure = _record(state=BackupState.FAILED, finished_at=now - timedelta(hours=30))
    fresh_failure = _record(state=BackupState.FAILED, finished_at=now - timedelta(hours=2))
    decision = _policy((keeper, old_failure, fresh_failure), now=now, failed_grace_hours=24)

    assert old_failure.id in decision.delete
    assert fresh_failure.id not in decision.delete
    assert fresh_failure.id not in decision.keep


def test_retention_failed_without_finish_uses_started_at() -> None:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    stuck = BackupRecord(
        id=uuid.uuid4(),
        started_at=now - timedelta(hours=48),
        state=BackupState.FAILED,
        schema_revision="rev",
        finished_at=None,
    )
    decision = _policy((stuck,), now=now, failed_grace_hours=24)
    assert stuck.id in decision.delete


def test_retention_ignores_running_records() -> None:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    running = _record(state=BackupState.RUNNING, finished_at=None)
    decision = _policy((running,), now=now)
    assert decision.keep == ()
    assert decision.delete == ()


def test_retention_empty_history() -> None:
    decision = _policy(())
    assert decision == RetentionDecision()


# --- is_stale --------------------------------------------------------------


def test_stale_when_never_verified() -> None:
    assert is_stale(None, max_age_hours=48, now=_at(0))


def test_stale_when_beyond_max_age() -> None:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    assert is_stale(now - timedelta(hours=49), max_age_hours=48, now=now)


def test_fresh_within_max_age() -> None:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    assert not is_stale(now - timedelta(hours=1), max_age_hours=48, now=now)


def test_fresh_exactly_at_boundary() -> None:
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    assert not is_stale(now - timedelta(hours=48), max_age_hours=48, now=now)
