"""No backup artifact may carry `MASTER_KEY`, in any form (task 11.11).

`backup-restore/spec.md`, "Master key is out of band": a backup must be useless
for reading encrypted content without the deployment key, which begins with the
key never appearing in an artifact at all. This test is adversarial -- it looks
for the key as raw bytes, base64, and hex, and also checks the other deployment
secrets -- against every value the pipeline serializes: the manifest JSON, the
persisted backup record, and the dump metadata.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import uuid
from datetime import UTC, datetime

from cyberfs.domain.backup import (
    BackupManifest,
    BackupRecord,
    ManifestEntry,
    SkewReport,
)
from cyberfs.domain.ports.backup import DumpResult
from tests.unit.conftest import make_settings

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)

#: A master key distinct from the other test defaults, so a hit is unambiguous.
MASTER_KEY_B64 = base64.b64encode(b"\x5a" * 32).decode()
MINIO_SECRET = "leak-me-minio-secret-value"
CLIENT_SECRET = "leak-me-client-secret-value"


def _secret_forms() -> list[str]:
    """Every spelling of every secret an artifact must never contain."""
    settings = make_settings(
        master_key=MASTER_KEY_B64,
        minio_secret_key=MINIO_SECRET,
        cyberfs_client_secret=CLIENT_SECRET,
    )
    key = settings.master_key_bytes
    forms = [
        MASTER_KEY_B64,
        base64.b64encode(key).decode(),
        key.hex(),
        key.decode("latin-1"),
        MINIO_SECRET,
        CLIENT_SECRET,
    ]
    return [form for form in forms if form]


def _manifest() -> BackupManifest:
    entries = tuple(
        ManifestEntry(
            key=f"{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}",
            size=1024,
            checksum=uuid.uuid4().hex,
        )
        for _ in range(5)
    )
    return BackupManifest.from_entries(
        entries,
        dump_checksum=uuid.uuid4().hex,
        dump_size=4096,
        schema_revision="079de84e4f44",
    )


def _record(manifest: BackupManifest) -> BackupRecord:
    started = BackupRecord.start(started_at=NOW, schema_revision=manifest.schema_revision)
    return started.mark_verified(finished_at=NOW, manifest=manifest, skew=SkewReport())


def test_manifest_json_carries_no_secret() -> None:
    blob = _manifest().to_json()
    for form in _secret_forms():
        assert form not in blob


def test_backup_record_carries_no_secret() -> None:
    manifest = _manifest()
    record = _record(manifest)
    serialized = json.dumps(dataclasses.asdict(record), default=str)
    for form in _secret_forms():
        assert form not in serialized


def test_dump_result_carries_no_secret() -> None:
    """The dump's own metadata is checksums and sizes only."""
    result = DumpResult(size=4096, checksum=uuid.uuid4().hex, schema_revision="079de84e4f44")
    serialized = json.dumps(dataclasses.asdict(result), default=str)
    for form in _secret_forms():
        assert form not in serialized


def test_manifest_has_no_field_that_could_hold_key_material() -> None:
    """The guarantee is structural: there is simply no field to put a key in."""
    manifest = _manifest()
    data = json.loads(manifest.to_json())
    assert set(data) == {
        "schema_revision",
        "dump_checksum",
        "dump_size",
        "object_count",
        "total_bytes",
        "entries",
    }
    for entry in data["entries"]:
        assert set(entry) == {"key", "size", "checksum"}
