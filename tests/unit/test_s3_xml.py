"""S3 XML serialization and error mapping -- `s3-compatibility/spec.md`.

Two invariants live here. First, every failure answers in the S3 ``<Error>``
shape with a `Code`, a `Message`, and a request id -- never RFC 7807
(invariant D). Second, no S3 response ever carries the object store endpoint, an
AWS signature for it, or an internal ``{owner}/{node}/{version}`` storage key --
only the user-facing key does (invariant E).
"""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree.ElementTree import fromstring

import pytest

from cyberfs.adapters.inbound.api.s3.errors import s3_error_response
from cyberfs.adapters.inbound.api.s3.xml import (
    S3_NAMESPACE,
    complete_multipart_upload_result,
    copy_object_result,
    delete_result,
    error_document,
    initiate_multipart_upload_result,
    list_all_my_buckets,
    list_bucket_result,
    list_parts_result,
    parse_complete_multipart_upload,
    parse_delete_request,
)
from cyberfs.application.s3_objects import BucketInfo, DeleteOutcome
from cyberfs.domain.errors import (
    InvalidArgumentError,
    NoSuchBucketError,
    NoSuchKeyError,
    PermissionDeniedError,
    QuotaExceededError,
    S3NotImplementedError,
)
from cyberfs.domain.s3.listing import ListRequest, ListResult, S3ObjectEntry
from cyberfs.domain.s3.multipart import MultipartPart

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _text(document: bytes, local: str) -> str | None:
    root = fromstring(document)  # noqa: S314 -- our own trusted, generated XML
    element = root.find(f"{{{S3_NAMESPACE}}}{local}")
    return element.text if element is not None else None


# --- error document --------------------------------------------------------


def test_error_document_carries_code_message_and_request_id() -> None:
    document = error_document("NoSuchKey", "the key is gone", request_id="req-42")
    assert _text(document, "Code") == "NoSuchKey"
    assert _text(document, "Message") == "the key is gone"
    assert _text(document, "RequestId") == "req-42"


def test_error_document_is_not_a_problem_document() -> None:
    document = error_document("AccessDenied", "no", request_id=None)
    body = document.decode("utf-8")
    assert "<Error" in body
    assert "problem+json" not in body
    assert "https://cyberfs" not in body


# --- error mapping ---------------------------------------------------------


def test_a_no_such_key_maps_to_404() -> None:
    response = s3_error_response(NoSuchKeyError("gone"))
    assert response.status_code == 404
    assert _text(response.body, "Code") == "NoSuchKey"


def test_a_quota_error_maps_to_403_quota_exceeded() -> None:
    response = s3_error_response(QuotaExceededError("full"))
    assert response.status_code == 403
    assert _text(response.body, "Code") == "QuotaExceeded"


def test_a_missing_bucket_maps_to_404_no_such_bucket() -> None:
    response = s3_error_response(NoSuchBucketError("nope", bucket="bob"))
    assert response.status_code == 404
    assert _text(response.body, "Code") == "NoSuchBucket"


def test_an_unsupported_operation_maps_to_501_not_implemented() -> None:
    response = s3_error_response(S3NotImplementedError("later"))
    assert response.status_code == 501
    assert _text(response.body, "Code") == "NotImplemented"


def test_a_bare_permission_error_falls_back_to_access_denied() -> None:
    response = s3_error_response(PermissionDeniedError("no"))
    assert response.status_code == 403
    assert _text(response.body, "Code") == "AccessDenied"


def test_an_unexpected_exception_is_a_generic_internal_error() -> None:
    response = s3_error_response(RuntimeError("stack trace with secrets"))
    assert response.status_code == 500
    assert _text(response.body, "Code") == "InternalError"
    assert b"secrets" not in response.body


def test_every_error_response_is_xml() -> None:
    response = s3_error_response(NoSuchKeyError("gone"))
    assert response.media_type == "application/xml"


# --- list buckets ----------------------------------------------------------


def test_list_all_my_buckets_names_the_single_bucket() -> None:
    document = list_all_my_buckets("alice", [BucketInfo(name="alice", created_at=NOW)])
    body = document.decode("utf-8")
    assert "<Name>alice</Name>" in body
    assert body.count("<Bucket>") == 1


# --- list objects ----------------------------------------------------------


def _result() -> ListResult:
    return ListResult(
        entries=(S3ObjectEntry(key="reports/q3.xlsx", size=21, etag='"abc-0"', last_modified=NOW),),
        common_prefixes=("reports/2024/",),
        is_truncated=True,
        next_token="dG9rZW4=",
        prefix="reports/",
        delimiter="/",
    )


def test_list_bucket_result_has_the_expected_shape() -> None:
    body = list_bucket_result(
        "alice", _result(), ListRequest(prefix="reports/", delimiter="/")
    ).decode()
    assert "<Key>reports/q3.xlsx</Key>" in body
    assert "<Size>21</Size>" in body
    assert "<CommonPrefixes><Prefix>reports/2024/</Prefix></CommonPrefixes>" in body
    assert "<IsTruncated>true</IsTruncated>" in body
    assert "<NextContinuationToken>dG9rZW4=</NextContinuationToken>" in body
    assert "<KeyCount>2</KeyCount>" in body


def test_list_bucket_result_leaks_no_storage_detail() -> None:
    # A storage key looks like "{owner-uuid}/{node-uuid}/{version-uuid}"; the
    # response must carry only the user-facing key, never that, and never the
    # object store endpoint or a signature for it.
    body = list_bucket_result("alice", _result(), ListRequest(prefix="reports/")).decode()
    assert "minio" not in body.lower()
    assert "AWS4-HMAC-SHA256" not in body
    # The user-facing key has exactly one path separator here; a storage key
    # would introduce UUID path segments, which are absent.
    assert "X-Amz" not in body


# --- copy / delete results -------------------------------------------------


def test_copy_object_result_carries_the_etag() -> None:
    body = copy_object_result('"copy-etag"', NOW).decode()
    assert "<CopyObjectResult" in body
    assert '<ETag>"copy-etag"</ETag>' in body
    assert "<LastModified>" in body


def test_delete_result_reports_successes_and_errors() -> None:
    outcomes = (
        DeleteOutcome(key="gone.txt"),
        DeleteOutcome(key="denied.txt", error_code="AccessDenied", error_message="no"),
    )
    body = delete_result(outcomes, quiet=False).decode()
    assert "<DeleteResult" in body
    assert "<Deleted><Key>gone.txt</Key></Deleted>" in body
    assert "<Key>denied.txt</Key>" in body
    assert "<Code>AccessDenied</Code>" in body


def test_delete_result_quiet_mode_omits_successes() -> None:
    outcomes = (
        DeleteOutcome(key="gone.txt"),
        DeleteOutcome(key="denied.txt", error_code="AccessDenied", error_message="no"),
    )
    body = delete_result(outcomes, quiet=True).decode()
    assert "<Deleted>" not in body
    assert "denied.txt" in body  # errors are always reported


# --- delete request parsing ------------------------------------------------


def test_parse_delete_request_extracts_keys_and_quiet() -> None:
    body = (
        b"<Delete><Quiet>true</Quiet>"
        b"<Object><Key>a.txt</Key></Object>"
        b"<Object><Key>b.txt</Key></Object></Delete>"
    )
    keys, quiet = parse_delete_request(body)
    assert keys == ("a.txt", "b.txt")
    assert quiet is True


def test_parse_delete_request_defaults_quiet_false() -> None:
    keys, quiet = parse_delete_request(b"<Delete><Object><Key>a.txt</Key></Object></Delete>")
    assert keys == ("a.txt",)
    assert quiet is False


def test_parse_delete_request_rejects_malformed_xml() -> None:
    with pytest.raises(InvalidArgumentError):
        parse_delete_request(b"not xml at all <<<")


# --- multipart serializers -------------------------------------------------


def test_initiate_result_carries_the_upload_id() -> None:
    body = initiate_multipart_upload_result("alice", "big/data.bin", "up-123").decode()
    assert "<InitiateMultipartUploadResult" in body
    assert "<Bucket>alice</Bucket>" in body
    assert "<Key>big/data.bin</Key>" in body
    assert "<UploadId>up-123</UploadId>" in body


def test_complete_result_location_is_the_cyberfs_endpoint() -> None:
    body = complete_multipart_upload_result(
        "https://files.example.test/s3/alice/big/data.bin", "alice", "big/data.bin", '"etag-1"'
    ).decode()
    assert "<CompleteMultipartUploadResult" in body
    assert "<Location>https://files.example.test/s3/alice/big/data.bin</Location>" in body
    assert '<ETag>"etag-1"</ETag>' in body
    # Never the object store, and no signature over it.
    assert "minio" not in body.lower()
    assert "X-Amz" not in body


def test_list_parts_result_lists_each_part() -> None:
    parts = (
        MultipartPart(
            upload_id="up-1",
            part_number=1,
            etag='"aaa"',
            size=10,
            object_key="multipart/up-1/1",
            uploaded_at=NOW,
        ),
        MultipartPart(
            upload_id="up-1",
            part_number=2,
            etag='"bbb"',
            size=20,
            object_key="multipart/up-1/2",
            uploaded_at=NOW,
        ),
    )
    body = list_parts_result("alice", "big.bin", "up-1", parts).decode()
    assert "<ListPartsResult" in body
    assert "<PartNumber>1</PartNumber>" in body
    assert '<ETag>"bbb"</ETag>' in body
    assert "<Size>20</Size>" in body
    # The staging object key is never disclosed.
    assert "multipart/up-1" not in body


def test_parse_complete_extracts_part_numbers_and_etags() -> None:
    body = (
        b"<CompleteMultipartUpload>"
        b"<Part><PartNumber>1</PartNumber><ETag>&quot;aaa&quot;</ETag></Part>"
        b"<Part><PartNumber>2</PartNumber><ETag>&quot;bbb&quot;</ETag></Part>"
        b"</CompleteMultipartUpload>"
    )
    assert parse_complete_multipart_upload(body) == ((1, '"aaa"'), (2, '"bbb"'))


def test_parse_complete_rejects_malformed_xml() -> None:
    with pytest.raises(InvalidArgumentError):
        parse_complete_multipart_upload(b"not xml <<<")


def test_parse_complete_rejects_an_empty_part_list() -> None:
    with pytest.raises(InvalidArgumentError):
        parse_complete_multipart_upload(b"<CompleteMultipartUpload></CompleteMultipartUpload>")


def test_parse_complete_rejects_a_non_numeric_part_number() -> None:
    body = (
        b"<CompleteMultipartUpload>"
        b"<Part><PartNumber>x</PartNumber><ETag>&quot;aaa&quot;</ETag></Part>"
        b"</CompleteMultipartUpload>"
    )
    with pytest.raises(InvalidArgumentError):
        parse_complete_multipart_upload(body)
