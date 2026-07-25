"""Backup value objects and pure policy.

A backup is a consistent Postgres dump plus a ciphertext-faithful mirror of the
content bucket, described by a manifest that lists every object with its size
and checksum -- and nothing else. `backup-restore/spec.md` requires that no
backup artifact ever carry `MASTER_KEY` in any form; the manifest's shape is the
first line of that guarantee, since there is no field here that could hold key
material.

Everything in this module is pure: manifest serialization, dump/mirror skew
detection, the grandfather-father-son retention policy, and staleness. The I/O
that produces and stores these values lives in the application and adapter
layers.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum


class BackupState(StrEnum):
    #: A run is in progress; not yet verified, not yet a candidate for retention.
    RUNNING = "running"
    #: Copy and verification both succeeded. Only these count toward retention.
    VERIFIED = "verified"
    #: Copy or verification failed. Pruned aggressively after the grace window.
    FAILED = "failed"
    #: A scheduled run that a still-running run pre-empted (overlap prevention).
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One mirrored object: an opaque key, its byte size, and its checksum.

    Deliberately holds no name, owner, or key material -- the key is the opaque
    storage key, never user text.
    """

    key: str
    size: int
    checksum: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """The integrity record for one backup.

    Lists every mirrored object plus the dump's checksum and the schema
    migration revision, so verification can confirm the copy without the
    originals -- see `backup-restore/spec.md`, "Integrity verification".
    """

    entries: tuple[ManifestEntry, ...]
    dump_checksum: str
    dump_size: int
    schema_revision: str
    object_count: int
    total_bytes: int

    @classmethod
    def from_entries(
        cls,
        entries: tuple[ManifestEntry, ...],
        *,
        dump_checksum: str,
        dump_size: int,
        schema_revision: str,
    ) -> BackupManifest:
        """Build a manifest, deriving the object count and total bytes."""
        entries = tuple(entries)
        return cls(
            entries=entries,
            dump_checksum=dump_checksum,
            dump_size=dump_size,
            schema_revision=schema_revision,
            object_count=len(entries),
            total_bytes=sum(entry.size for entry in entries),
        )

    def to_json(self) -> str:
        """Serialize to canonical JSON.

        Only keys, sizes, and checksums are emitted -- there is no field here
        that could carry a secret, which is what makes the "no key in any
        artifact" invariant hold by construction.
        """
        return json.dumps(
            {
                "schema_revision": self.schema_revision,
                "dump_checksum": self.dump_checksum,
                "dump_size": self.dump_size,
                "object_count": self.object_count,
                "total_bytes": self.total_bytes,
                "entries": [
                    {"key": entry.key, "size": entry.size, "checksum": entry.checksum}
                    for entry in self.entries
                ],
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> BackupManifest:
        data = json.loads(raw)
        entries = tuple(
            ManifestEntry(key=item["key"], size=item["size"], checksum=item["checksum"])
            for item in data["entries"]
        )
        return cls(
            entries=entries,
            dump_checksum=data["dump_checksum"],
            dump_size=data["dump_size"],
            schema_revision=data["schema_revision"],
            object_count=data["object_count"],
            total_bytes=data["total_bytes"],
        )

    def sample(self, count: int, *, seed: str) -> tuple[ManifestEntry, ...]:
        """A deterministic subset of entries for verification.

        Deterministic in `seed` so a re-run samples the same objects, which
        makes an intermittent verification failure reproducible.
        """
        if count <= 0:
            return ()
        if count >= len(self.entries):
            return self.entries
        # Not security-sensitive: this only chooses which already-public object
        # keys to re-checksum during verification. Determinism matters more than
        # unpredictability, hence a seeded PRNG.
        rng = random.Random(seed)  # noqa: S311
        return tuple(rng.sample(self.entries, count))


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """The durable history of one backup run.

    Failures are kept, not discarded: `backup-restore/spec.md` requires history
    to cover failures so an operator can see a run that never succeeded.
    """

    id: uuid.UUID
    started_at: datetime
    state: BackupState
    schema_revision: str
    finished_at: datetime | None = None
    dump_checksum: str | None = None
    object_count: int = 0
    total_bytes: int = 0
    skew_missing_in_dump: int = 0
    skew_missing_in_manifest: int = 0
    error: str | None = None

    @classmethod
    def start(
        cls,
        *,
        started_at: datetime,
        schema_revision: str,
        backup_id: uuid.UUID | None = None,
    ) -> BackupRecord:
        return cls(
            id=backup_id or uuid.uuid4(),
            started_at=started_at,
            state=BackupState.RUNNING,
            schema_revision=schema_revision,
        )

    @property
    def is_verified(self) -> bool:
        return self.state is BackupState.VERIFIED

    @property
    def is_failed(self) -> bool:
        return self.state is BackupState.FAILED

    @property
    def has_skew(self) -> bool:
        return self.skew_missing_in_dump > 0 or self.skew_missing_in_manifest > 0

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def bucket_prefix(self) -> str:
        """Where this backup's artifacts live in the target bucket."""
        return f"backups/{self.id}"

    @property
    def dump_key(self) -> str:
        return f"{self.bucket_prefix}/dump.sql.gz"

    @property
    def manifest_key(self) -> str:
        return f"{self.bucket_prefix}/manifest.json"

    def mark_verified(
        self,
        *,
        finished_at: datetime,
        manifest: BackupManifest,
        skew: SkewReport,
    ) -> BackupRecord:
        return replace(
            self,
            state=BackupState.VERIFIED,
            finished_at=finished_at,
            dump_checksum=manifest.dump_checksum,
            object_count=manifest.object_count,
            total_bytes=manifest.total_bytes,
            skew_missing_in_dump=len(skew.missing_in_dump),
            skew_missing_in_manifest=len(skew.missing_in_manifest),
            error=None,
        )

    def mark_failed(self, *, finished_at: datetime, error: str) -> BackupRecord:
        return replace(self, state=BackupState.FAILED, finished_at=finished_at, error=error)


@dataclass(frozen=True, slots=True)
class SkewReport:
    """Objects the manifest and the dump disagree about.

    `missing_in_dump` names objects the mirror captured that the metadata dump
    does not reference; `missing_in_manifest` names the reverse. Both exclude a
    tolerated in-flight window -- an upload landing mid-backup is expected drift,
    not corruption.
    """

    missing_in_dump: tuple[str, ...] = ()
    missing_in_manifest: tuple[str, ...] = ()

    @property
    def has_skew(self) -> bool:
        return bool(self.missing_in_dump or self.missing_in_manifest)


def detect_skew(
    manifest_keys: frozenset[str],
    dump_referenced_keys: frozenset[str],
    *,
    in_flight: frozenset[str] = frozenset(),
) -> SkewReport:
    """Compare what the mirror captured against what the dump references.

    An object in the manifest but not referenced by the dump, or referenced by
    the dump but absent from the manifest, is skew -- unless it fell in the
    tolerated in-flight window, in which case it is expected and ignored.
    """
    missing_in_dump = sorted((manifest_keys - dump_referenced_keys) - in_flight)
    missing_in_manifest = sorted((dump_referenced_keys - manifest_keys) - in_flight)
    return SkewReport(tuple(missing_in_dump), tuple(missing_in_manifest))


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    """The outcome of applying the retention policy to the history."""

    keep: tuple[uuid.UUID, ...] = ()
    delete: tuple[uuid.UUID, ...] = ()
    protected_last_verified: bool = False


def _period_of(moment: datetime, period: str) -> tuple[int, ...]:
    """A bucket key for grandfather-father-son grouping."""
    if period == "day":
        return (moment.year, moment.month, moment.day)
    if period == "week":
        iso = moment.isocalendar()
        return (iso.year, iso.week)
    return (moment.year, moment.month)


def _bucket_ids(records: tuple[BackupRecord, ...], *, period: str, count: int) -> set[uuid.UUID]:
    """The newest record in each of the most recent `count` periods.

    `records` must be newest-first. One backup per period is retained, for the
    N most recent periods -- classic GFS.
    """
    kept: set[uuid.UUID] = set()
    seen: list[tuple[int, ...]] = []
    for record in records:
        if record.finished_at is None:  # pragma: no cover -- callers pass finished records
            continue
        key = _period_of(record.finished_at, period)
        if key in seen:
            continue
        if len(seen) >= count:
            break
        seen.append(key)
        kept.add(record.id)
    return kept


def _finished_at(record: BackupRecord) -> datetime:
    """Sort key for verified records, which always carry a finish time."""
    assert record.finished_at is not None  # guarded by the caller
    return record.finished_at


def decide_retention(
    records: tuple[BackupRecord, ...],
    *,
    keep_daily: int,
    keep_weekly: int,
    keep_monthly: int,
    now: datetime,
    failed_grace_hours: int,
) -> RetentionDecision:
    """Apply the retention policy, never deleting the last verified backup.

    Keeps the newest daily, weekly, and monthly verified backups; deletes the
    verified backups outside every bucket and the failed backups older than the
    grace window. The most recent verified backup is always retained even if no
    bucket would have selected it -- the caller logs a warning in that case.
    """
    verified = tuple(
        sorted(
            (r for r in records if r.is_verified and r.finished_at is not None),
            key=_finished_at,
            reverse=True,
        )
    )
    keep_ids = _bucket_ids(verified, period="day", count=keep_daily)
    keep_ids |= _bucket_ids(verified, period="week", count=keep_weekly)
    keep_ids |= _bucket_ids(verified, period="month", count=keep_monthly)

    protected = False
    if verified and verified[0].id not in keep_ids:
        keep_ids.add(verified[0].id)
        protected = True

    delete_ids = [r.id for r in verified if r.id not in keep_ids]
    delete_ids.extend(_expired_failed_ids(records, now=now, grace_hours=failed_grace_hours))

    keep_order = [r.id for r in verified if r.id in keep_ids]
    return RetentionDecision(
        keep=tuple(keep_order),
        delete=tuple(delete_ids),
        protected_last_verified=protected,
    )


def _expired_failed_ids(
    records: tuple[BackupRecord, ...], *, now: datetime, grace_hours: int
) -> list[uuid.UUID]:
    grace = timedelta(hours=grace_hours)
    expired: list[uuid.UUID] = []
    for record in records:
        if not record.is_failed:
            continue
        reference = record.finished_at or record.started_at
        if now - reference >= grace:
            expired.append(record.id)
    return expired


def is_stale(last_verified_at: datetime | None, *, max_age_hours: int, now: datetime) -> bool:
    """Whether no verified backup has completed within the freshness window.

    Drives the staleness alert on the health endpoint. A history with no
    verified backup at all is stale by definition.
    """
    if last_verified_at is None:
        return True
    return now - last_verified_at > timedelta(hours=max_age_hours)
