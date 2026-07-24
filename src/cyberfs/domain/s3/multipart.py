"""Pure multipart-upload logic.

A multipart upload stages its parts as temporary objects and records each in a
metadata row; on completion the parts are concatenated in part-number order
into a single upload, so the result equals their concatenation
(`s3-compatibility/spec.md`, "Multipart upload"). Everything here is pure: the
staging-key grammar, the S3 ETag shapes, part ordering and validation, and the
abandonment cutoff. Storage and the DB rows live behind ports; the single
concatenating upload that charges quota once lives in the application service.

The staging prefix is deliberately outside the ``{owner}/{node}/{version}``
object-key shape the orphan reaper recognises, so a staged part is never
mistaken for a live version and swept early -- abandoned uploads are reclaimed
by their own metadata-driven sweep instead.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from cyberfs.domain.errors import InvalidPartError, InvalidPartOrderError
from cyberfs.domain.s3.namespace import S3Target

#: The reserved first segment every staged part key lives under. Chosen so a
#: staged part never matches the live-object key pattern the reaper scans.
STAGING_PREFIX = "multipart"

#: S3 permits part numbers 1..10000; anything else is rejected as an argument.
MIN_PART_NUMBER = 1
MAX_PART_NUMBER = 10_000


@dataclass(frozen=True, slots=True)
class MultipartUpload:
    """One in-flight multipart upload.

    The target is stored decomposed so completion can rebuild the exact
    `S3Target` the write descends -- the caller's own tree or a shared subtree
    -- without re-parsing a raw key. `initiator_subject` scopes the upload to
    the subject that created it, so only they can add to, complete, or abort it.
    """

    upload_id: str
    initiator_subject: str
    target_owner_subject: str
    target_key: str
    via_shared: bool
    content_type: str | None
    created_at: datetime

    @property
    def target(self) -> S3Target:
        """Rebuild the write target completion descends into."""
        return S3Target(
            owner_subject=self.target_owner_subject,
            path=self.target_key,
            via_shared=self.via_shared,
        )


@dataclass(frozen=True, slots=True)
class MultipartPart:
    """One staged part: its number, its S3 ETag, its size, and where it lives."""

    upload_id: str
    part_number: int
    etag: str
    size: int
    object_key: str
    uploaded_at: datetime


def staged_prefix(upload_id: str) -> str:
    """The reserved prefix every part of one upload is staged under."""
    return f"{STAGING_PREFIX}/{upload_id}/"


def staged_part_key(upload_id: str, part_number: int) -> str:
    """The object-store key one part is staged at."""
    return f"{staged_prefix(upload_id)}{part_number}"


def part_etag(md5_hex: str) -> str:
    """The ETag `UploadPart` returns -- the part's MD5, quoted, as S3 does."""
    return f'"{md5_hex}"'


def multipart_etag(part_etags: Sequence[str]) -> str:
    """The composite ETag S3 reports for a completed multipart object.

    It is the MD5 of the concatenated binary MD5 digests of the parts, suffixed
    with ``-N`` for the part count -- not the object's own content hash.
    """
    digest = hashlib.md5(usedforsecurity=False)
    for etag in part_etags:
        digest.update(bytes.fromhex(_md5_hex(etag)))
    return f'"{digest.hexdigest()}-{len(part_etags)}"'


def order_parts(parts: Sequence[MultipartPart]) -> tuple[MultipartPart, ...]:
    """Sort staged parts by part number, rejecting a duplicated number.

    A duplicate would make the assembled order ambiguous, so it is an
    `InvalidPartOrder` rather than a silent last-writer-wins.
    """
    ordered = sorted(parts, key=lambda part: part.part_number)
    seen: set[int] = set()
    for part in ordered:
        if part.part_number in seen:
            raise InvalidPartOrderError("a part number is staged more than once")
        seen.add(part.part_number)
    return tuple(ordered)


def reconcile_requested(
    requested: Sequence[tuple[int, str]], staged: Sequence[MultipartPart]
) -> tuple[MultipartPart, ...]:
    """Validate the client's completion part list against what was staged.

    Every requested part must exist with a matching ETag, and the numbers must
    be strictly ascending so the concatenation order is unambiguous. Returns the
    staged parts in the requested order, ready to assemble.
    """
    if not requested:
        raise InvalidPartError("the completion request lists no parts")
    by_number = {part.part_number: part for part in staged}
    chosen: list[MultipartPart] = []
    last = 0
    for number, etag in requested:
        if number <= last:
            raise InvalidPartOrderError("parts must be listed in ascending order")
        last = number
        part = by_number.get(number)
        if part is None or _md5_hex(part.etag) != _md5_hex(etag):
            raise InvalidPartError("a listed part is missing or its ETag did not match")
        chosen.append(part)
    return tuple(chosen)


def is_abandoned(upload: MultipartUpload, now: datetime, abandon: timedelta) -> bool:
    """Whether an upload has outlived the abandonment window and may be reaped."""
    return now - upload.created_at >= abandon


def _md5_hex(etag: str) -> str:
    """The bare MD5 hex of an ETag, tolerating quotes and a ``-N`` suffix."""
    return etag.strip('"').split("-", 1)[0]
