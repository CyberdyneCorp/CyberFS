"""Pure multipart logic -- `s3-compatibility/spec.md`, "Multipart upload".

Exercises the staging-key grammar, the S3 ETag shapes, part ordering and
validation, and the abandonment cutoff -- all pure, no I/O.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.domain.errors import InvalidPartError, InvalidPartOrderError
from cyberfs.domain.s3.multipart import (
    STAGING_PREFIX,
    MultipartPart,
    MultipartUpload,
    is_abandoned,
    multipart_etag,
    order_parts,
    part_etag,
    reconcile_requested,
    staged_part_key,
    staged_prefix,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _part(number: int, etag: str = '"deadbeef"', size: int = 8) -> MultipartPart:
    return MultipartPart(
        upload_id="up-1",
        part_number=number,
        etag=etag,
        size=size,
        object_key=staged_part_key("up-1", number),
        uploaded_at=NOW,
    )


# --- staging keys ----------------------------------------------------------


def test_staged_keys_live_under_the_reserved_prefix() -> None:
    assert staged_prefix("up-1") == f"{STAGING_PREFIX}/up-1/"
    assert staged_part_key("up-1", 3) == f"{STAGING_PREFIX}/up-1/3"


def test_a_staged_key_is_not_a_live_object_key() -> None:
    # The reaper's live-object key is `{owner}/{node}/{version}` uuids; a staged
    # key begins with the reserved prefix and never matches that shape.
    key = staged_part_key("abc123", 1)
    assert key.startswith(f"{STAGING_PREFIX}/")


# --- etags -----------------------------------------------------------------


def test_part_etag_is_the_quoted_md5() -> None:
    md5 = hashlib.md5(b"hello", usedforsecurity=False).hexdigest()
    assert part_etag(md5) == f'"{md5}"'


def test_multipart_etag_is_the_composite_of_part_md5s() -> None:
    a = hashlib.md5(b"a", usedforsecurity=False).hexdigest()
    b = hashlib.md5(b"b", usedforsecurity=False).hexdigest()
    composite = multipart_etag([part_etag(a), part_etag(b)])
    expected = hashlib.md5(bytes.fromhex(a) + bytes.fromhex(b), usedforsecurity=False).hexdigest()
    assert composite == f'"{expected}-2"'


# --- ordering --------------------------------------------------------------


def test_order_parts_sorts_by_part_number() -> None:
    ordered = order_parts([_part(3), _part(1), _part(2)])
    assert [p.part_number for p in ordered] == [1, 2, 3]


def test_order_parts_rejects_a_duplicate_number() -> None:
    with pytest.raises(InvalidPartOrderError):
        order_parts([_part(1), _part(1)])


# --- reconciliation --------------------------------------------------------


def test_reconcile_returns_staged_parts_in_requested_order() -> None:
    staged = [_part(1, '"aaa"'), _part(2, '"bbb"')]
    chosen = reconcile_requested([(1, '"aaa"'), (2, '"bbb"')], staged)
    assert [p.part_number for p in chosen] == [1, 2]


def test_reconcile_tolerates_unquoted_etags() -> None:
    staged = [_part(1, '"aaa"')]
    chosen = reconcile_requested([(1, "aaa")], staged)
    assert chosen[0].part_number == 1


def test_reconcile_rejects_a_missing_part() -> None:
    with pytest.raises(InvalidPartError):
        reconcile_requested([(1, '"aaa"'), (2, '"bbb"')], [_part(1, '"aaa"')])


def test_reconcile_rejects_a_mismatched_etag() -> None:
    with pytest.raises(InvalidPartError):
        reconcile_requested([(1, '"wrong"')], [_part(1, '"aaa"')])


def test_reconcile_rejects_a_non_ascending_list() -> None:
    staged = [_part(1, '"aaa"'), _part(2, '"bbb"')]
    with pytest.raises(InvalidPartOrderError):
        reconcile_requested([(2, '"bbb"'), (1, '"aaa"')], staged)


def test_reconcile_rejects_an_empty_list() -> None:
    with pytest.raises(InvalidPartError):
        reconcile_requested([], [_part(1)])


# --- abandonment -----------------------------------------------------------


def _upload(created_at: datetime) -> MultipartUpload:
    return MultipartUpload(
        upload_id="up-1",
        initiator_subject="alice",
        target_owner_subject="alice",
        target_key="/big.bin",
        via_shared=False,
        content_type=None,
        created_at=created_at,
    )


def test_an_upload_past_the_window_is_abandoned() -> None:
    upload = _upload(NOW - timedelta(hours=25))
    assert is_abandoned(upload, NOW, timedelta(hours=24)) is True


def test_an_upload_inside_the_window_is_not_abandoned() -> None:
    upload = _upload(NOW - timedelta(hours=1))
    assert is_abandoned(upload, NOW, timedelta(hours=24)) is False


def test_the_target_property_rebuilds_the_write_address() -> None:
    upload = MultipartUpload(
        upload_id="up-1",
        initiator_subject="alice",
        target_owner_subject="bob",
        target_key="/projects/big.bin",
        via_shared=True,
        content_type="application/octet-stream",
        created_at=NOW,
    )
    target = upload.target
    assert target.owner_subject == "bob"
    assert target.path == "/projects/big.bin"
    assert target.via_shared is True
