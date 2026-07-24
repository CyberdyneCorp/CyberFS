"""S3 XML response serialization.

Standard S3 clients parse a fixed XML dialect, namespaced
``http://s3.amazonaws.com/doc/2006-03-01/``. This module renders exactly the
documents the read path needs -- ``ListAllMyBucketsResult``,
``ListBucketResult``, and the ``<Error>`` document -- with stdlib
``xml.etree.ElementTree`` and no third-party library.

Leak boundary (`file-storage/spec.md`, "The object store endpoint never appears
in a response"): every value serialized here is user-facing -- the bucket name
(the caller's own subject), the object *key* (their path, never the
``{owner}/{node}/{version}`` storage key), the plaintext size, the derived
ETag, and timestamps. The MinIO endpoint and any AWS signature for it are never
in scope for a serializer that only ever sees domain values.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

from cyberfs.application.s3_objects import BucketInfo, DeleteOutcome
from cyberfs.domain.errors import InvalidArgumentError
from cyberfs.domain.s3.listing import ListRequest, ListResult

#: The one S3 XML namespace every document declares.
S3_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"


def error_document(
    code: str, message: str, *, request_id: str | None = None, resource: str | None = None
) -> bytes:
    """An S3 ``<Error>`` document: a `Code`, a `Message`, and the request id."""
    root = _root("Error")
    _text(root, "Code", code)
    _text(root, "Message", message)
    if resource is not None:
        _text(root, "Resource", resource)
    _text(root, "RequestId", request_id or "")
    return _render(root)


def list_all_my_buckets(owner_id: str, buckets: Sequence[BucketInfo]) -> bytes:
    """A ``ListAllMyBucketsResult`` -- exactly the caller's own bucket."""
    root = _root("ListAllMyBucketsResult")
    owner = SubElement(root, "Owner")
    _text(owner, "ID", owner_id)
    _text(owner, "DisplayName", owner_id)
    container = SubElement(root, "Buckets")
    for info in buckets:
        element = SubElement(container, "Bucket")
        _text(element, "Name", info.name)
        _text(element, "CreationDate", _iso(info.created_at))
    return _render(root)


def list_bucket_result(bucket: str, result: ListResult, req: ListRequest) -> bytes:
    """A ``ListBucketResult`` for `ListObjectsV2`, echoing the request parameters."""
    root = _root("ListBucketResult")
    _text(root, "Name", bucket)
    _text(root, "Prefix", result.prefix)
    if result.delimiter:
        _text(root, "Delimiter", result.delimiter)
    _text(root, "MaxKeys", str(req.max_keys))
    _text(root, "KeyCount", str(result.key_count))
    _text(root, "IsTruncated", _bool(result.is_truncated))
    if req.continuation_token is not None:
        _text(root, "ContinuationToken", req.continuation_token)
    if result.next_token is not None:
        _text(root, "NextContinuationToken", result.next_token)
    if req.start_after is not None:
        _text(root, "StartAfter", req.start_after)
    for entry in result.entries:
        contents = SubElement(root, "Contents")
        _text(contents, "Key", entry.key)
        _text(contents, "LastModified", _iso(entry.last_modified))
        _text(contents, "ETag", entry.etag)
        _text(contents, "Size", str(entry.size))
        _text(contents, "StorageClass", entry.storage_class)
    for prefix in result.common_prefixes:
        group = SubElement(root, "CommonPrefixes")
        _text(group, "Prefix", prefix)
    return _render(root)


def copy_object_result(etag: str, last_modified: datetime) -> bytes:
    """A ``CopyObjectResult`` -- the copy's ETag and modification time."""
    root = _root("CopyObjectResult")
    _text(root, "ETag", etag)
    _text(root, "LastModified", _iso(last_modified))
    return _render(root)


def delete_result(outcomes: Sequence[DeleteOutcome], *, quiet: bool) -> bytes:
    """A ``DeleteResult`` -- a `Deleted` per success, an `Error` per refusal.

    In quiet mode a client asked to hear only about failures, so successful
    keys are omitted; errors are always reported.
    """
    root = _root("DeleteResult")
    for outcome in outcomes:
        if outcome.deleted:
            if not quiet:
                _text(SubElement(root, "Deleted"), "Key", outcome.key)
        else:
            error = SubElement(root, "Error")
            _text(error, "Key", outcome.key)
            _text(error, "Code", outcome.error_code or "AccessDenied")
            _text(error, "Message", outcome.error_message or "")
    return _render(root)


def parse_delete_request(body: bytes) -> tuple[tuple[str, ...], bool]:
    """Parse a ``<Delete>`` batch request into its keys and quiet flag.

    A malformed body is an `InvalidArgument`, never a server error: the batch
    document is client input and is parsed defensively.
    """
    try:
        root = fromstring(body)  # noqa: S314 -- keys are treated as opaque text, no entity use
    except Exception as exc:  # a non-XML or truncated body
        raise InvalidArgumentError("the delete request is not valid XML") from exc
    keys: list[str] = []
    quiet = False
    for child in root:
        name = _local(child.tag)
        if name == "Object":
            keys.extend(_object_keys(child))
        elif name == "Quiet" and (child.text or "").strip().lower() == "true":
            quiet = True
    return tuple(keys), quiet


def _object_keys(obj: Element) -> list[str]:
    """Every non-empty ``<Key>`` under one ``<Object>`` of a delete batch."""
    return [child.text for child in obj if _local(child.tag) == "Key" and child.text]


def _local(tag: str) -> str:
    """The local name of a possibly namespaced tag."""
    return tag.rpartition("}")[2]


def _root(tag: str) -> Element:
    element = Element(tag)
    element.set("xmlns", S3_NAMESPACE)
    return element


def _text(parent: Element, tag: str, value: str) -> None:
    SubElement(parent, tag).text = value


def _render(root: Element) -> bytes:
    rendered: bytes = tostring(root, encoding="utf-8", xml_declaration=True)
    return rendered


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _iso(moment: datetime) -> str:
    """S3's ISO-8601 timestamp, always in UTC with millisecond precision."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
