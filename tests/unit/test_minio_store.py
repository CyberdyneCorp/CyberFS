"""Bucket provisioning contract for the MinIO adapter.

Only bucket lifecycle is exercised here with a fake client; the streaming and
range behaviours that need a real server live in tests/integration.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from minio.commonconfig import ENABLED
from minio.error import S3Error
from minio.versioningconfig import VersioningConfig

from cyberfs.adapters.outbound.objects.minio_store import MinioObjectStore
from cyberfs.domain.errors import DependencyUnavailableError

BUCKET = "cyberfs-content"


def _store(client: MagicMock) -> MinioObjectStore:
    return MinioObjectStore(client, BUCKET)


async def test_ensure_bucket_enables_versioning_on_create() -> None:
    client = MagicMock()
    client.bucket_exists.return_value = False

    await _store(client).ensure_bucket()

    client.make_bucket.assert_called_once_with(BUCKET)
    # Regression: ensure_bucket used to create the bucket without ever calling
    # set_bucket_versioning, so the spec's "versioning enabled" was unmet.
    client.set_bucket_versioning.assert_called_once()
    args = client.set_bucket_versioning.call_args.args
    assert args[0] == BUCKET
    config = args[1]
    assert isinstance(config, VersioningConfig)
    assert config.status == ENABLED


async def test_ensure_bucket_enables_versioning_when_already_present() -> None:
    client = MagicMock()
    client.bucket_exists.return_value = True

    await _store(client).ensure_bucket()

    client.make_bucket.assert_not_called()
    # Idempotent: versioning is reasserted even on an existing bucket.
    client.set_bucket_versioning.assert_called_once()


async def test_ensure_bucket_wraps_s3_error() -> None:
    client = MagicMock()
    client.bucket_exists.side_effect = S3Error(
        code="InternalError",
        message="boom",
        resource=BUCKET,
        request_id="r",
        host_id="h",
        response=None,
    )

    with pytest.raises(DependencyUnavailableError):
        await _store(client).ensure_bucket()
