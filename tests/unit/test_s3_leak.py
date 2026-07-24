"""No surface leaks the object store, a signature over it, or an object key.

The sharpened `file-storage`/`s3-compatibility` rule (invariant D, task 9.4):
no response from *any* surface may contain the configured MinIO endpoint, an
AWS signature *for it*, or an internal ``{owner}/{node}/{version}`` object key.
The precise distinction 9.5 draws is that a CyberFS-endpoint presigned URL --
one that carries an ``X-Amz-Signature`` but points at CyberFS and is bound to a
CyberFS access key -- is explicitly *permitted*, because the bytes still transit
CyberFS. What is banned is the MinIO host and the internal object-key shape.

These assertions run against the representative documents the S3 surface emits
and against a generated presigned URL, at the unit level, so a regression is
caught without a live stack.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest

from cyberfs.adapters.inbound.api.s3.xml import (
    complete_multipart_upload_result,
    copy_object_result,
    initiate_multipart_upload_result,
    list_all_my_buckets,
    list_bucket_result,
    list_parts_result,
)
from cyberfs.application.s3_objects import BucketInfo
from cyberfs.application.s3_presign import S3PresignService
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.domain.s3.listing import ListRequest, ListResult, S3ObjectEntry
from cyberfs.domain.s3.multipart import MultipartPart

from .fakes import FakeKeyProvider

#: A configured object-store endpoint that must never surface in any response.
MINIO_ENDPOINT = "minio.internal:9000"

#: The internal object-key shape -- three UUIDs -- that names raw stored bytes.
#: No response may carry it; the S3 surface only ever emits user-facing keys.
OBJECT_KEY_PATTERN = re.compile(r"[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f-]{36}", re.IGNORECASE)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
CYBERFS_ENDPOINT = "https://s3.cyberfs.example"
OWNER = "user-1"


def _sample_documents() -> list[bytes]:
    """One of every S3 XML document the surface produces."""
    buckets = list_all_my_buckets(OWNER, (BucketInfo(name=OWNER, created_at=NOW),))
    listing = list_bucket_result(
        OWNER,
        ListResult(
            entries=(
                S3ObjectEntry(key="reports/q3.xlsx", size=10, etag='"abc"', last_modified=NOW),
            ),
            common_prefixes=("reports/",),
            is_truncated=False,
            next_token=None,
            prefix="",
            delimiter="/",
        ),
        ListRequest(),
    )
    copied = copy_object_result('"deadbeef"', NOW)
    initiated = initiate_multipart_upload_result(OWNER, "big.bin", "upload-123")
    # A completion Location that addresses CyberFS's own endpoint, never MinIO.
    completed = complete_multipart_upload_result(
        f"{CYBERFS_ENDPOINT}/s3/{OWNER}/big.bin", OWNER, "big.bin", '"etag-2"'
    )
    parts = list_parts_result(
        OWNER,
        "big.bin",
        "upload-123",
        (
            MultipartPart(
                upload_id="upload-123",
                part_number=1,
                etag='"p1"',
                size=5,
                object_key="multipart/upload-123/1",
                uploaded_at=NOW,
            ),
        ),
    )
    return [buckets, listing, copied, initiated, completed, parts]


def _presigned_url() -> str:
    provider = FakeKeyProvider()
    service = S3PresignService(
        provider, endpoint=CYBERFS_ENDPOINT, base_path="/s3", region="us-east-1"
    )
    key = S3AccessKey(
        key_id="AKIATEST0000000000CI",
        sealed_secret=provider.seal_secret(b"the-signing-secret"),
        secret_master_key_id=provider.master_key_id,
        label="ci",
        owner_id=uuid.uuid4(),
        owner_subject=OWNER,
        created_at=NOW,
    )
    return service.presign(key, "GET", OWNER, "reports/q3.xlsx", expires=900, now=NOW)


# --- the object store is never disclosed -----------------------------------


def test_no_xml_document_contains_the_object_store_endpoint() -> None:
    for document in _sample_documents():
        assert MINIO_ENDPOINT.encode() not in document
        assert b"minio" not in document.lower()


def test_no_xml_document_contains_an_internal_object_key() -> None:
    for document in _sample_documents():
        assert not OBJECT_KEY_PATTERN.search(document.decode("utf-8")), document


def test_the_completion_location_addresses_cyberfs_not_the_store() -> None:
    completed = complete_multipart_upload_result(
        f"{CYBERFS_ENDPOINT}/s3/{OWNER}/big.bin", OWNER, "big.bin", '"etag"'
    ).decode("utf-8")
    assert CYBERFS_ENDPOINT in completed
    assert MINIO_ENDPOINT not in completed


# --- the presigned URL is a CyberFS URL, and only that ---------------------


def test_a_presigned_url_points_at_cyberfs_and_carries_no_store_reference() -> None:
    url = _presigned_url()
    assert urlsplit(url).netloc == urlsplit(CYBERFS_ENDPOINT).netloc
    lowered = url.lower()
    assert MINIO_ENDPOINT.lower() not in lowered
    assert "minio" not in lowered
    assert not OBJECT_KEY_PATTERN.search(url)


def test_a_presigned_url_is_allowed_to_carry_an_amz_signature() -> None:
    # The sharpened rule: an X-Amz-Signature is permitted precisely because the
    # URL points at CyberFS. What is banned is a MinIO host or a store credential.
    url = _presigned_url()
    assert "X-Amz-Signature=" in url
    assert "X-Amz-Credential=" in url
    # The credential names the CyberFS access key, not a MinIO one.
    assert "AKIATEST0000000000CI" in url


def test_the_presign_service_refuses_to_construct_without_an_endpoint() -> None:
    # A URL is never minted without a CyberFS endpoint to root it at.
    with pytest.raises(ValueError, match="CyberFS S3 endpoint is required"):
        S3PresignService(FakeKeyProvider(), endpoint="", base_path="/s3", region="us-east-1")
