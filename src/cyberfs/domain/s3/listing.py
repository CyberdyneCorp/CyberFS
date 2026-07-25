"""The pure `ListObjectsV2` algorithm and its continuation cursor.

The inbound adapter gathers the caller's authorized view -- every file they may
read, keyed as the user-facing path -- and hands that flat list here. This
module applies S3's prefix, delimiter, `CommonPrefixes`, `max-keys`, and
continuation-token semantics with no I/O, so the grouping and pagination are
unit-testable without a tree, a repository, or FastAPI.

The continuation token is an opaque, tamper-evident-enough resume cursor: it is
the base64url of the last key returned, and it only ever resumes a walk over the
*same* authorized view. A forged token cannot widen access -- at worst it
resumes at a different point in what the caller could already see -- so a
malformed one is simply rejected with `InvalidArgument` rather than trusted.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from cyberfs.domain.errors import InvalidArgumentError

#: AWS caps a single `ListObjectsV2` page at 1000 keys; CyberFS matches it so a
#: standard client's paging loop terminates exactly as it does against S3.
MAX_KEYS = 1000


@dataclass(frozen=True, slots=True)
class ListRequest:
    """The parameters of one `ListObjectsV2` call, already parsed and bounded."""

    prefix: str = ""
    delimiter: str = ""
    max_keys: int = MAX_KEYS
    continuation_token: str | None = None
    start_after: str | None = None


@dataclass(frozen=True, slots=True)
class S3ObjectEntry:
    """One listable object: the user-facing key and its plaintext metadata."""

    key: str
    size: int
    etag: str
    last_modified: datetime
    storage_class: str = "STANDARD"


@dataclass(frozen=True, slots=True)
class ListResult:
    """The outcome of a `ListObjectsV2`, ready for the XML serializer."""

    entries: tuple[S3ObjectEntry, ...]
    common_prefixes: tuple[str, ...]
    is_truncated: bool
    next_token: str | None
    prefix: str
    delimiter: str

    @property
    def key_count(self) -> int:
        """Objects plus common prefixes, as S3's `KeyCount` reports."""
        return len(self.entries) + len(self.common_prefixes)


def encode_token(key: str) -> str:
    """The opaque continuation token that resumes a listing after `key`."""
    return base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii")


def decode_token(token: str) -> str:
    """Recover the resume key from a token, or refuse a malformed one."""
    try:
        return base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidArgumentError("continuation token is not valid") from exc


def list_objects_v2(objects: Sequence[S3ObjectEntry], req: ListRequest) -> ListResult:
    """Apply prefix, delimiter, pagination, and grouping to a flat object list.

    `objects` is the caller's *entire* authorized view addressed by the request;
    every filtering and grouping decision is made here, so an object outside the
    prefix -- or rolled up into a `CommonPrefix` by the delimiter -- is handled
    identically to S3 without the gather step needing to know the rules.
    """
    contents, prefixes = _group(objects, req.prefix, req.delimiter)
    return _paginate(contents, prefixes, req)


def _group(
    objects: Sequence[S3ObjectEntry], prefix: str, delimiter: str
) -> tuple[dict[str, S3ObjectEntry], set[str]]:
    """Split matching objects into direct contents and rolled-up prefixes."""
    contents: dict[str, S3ObjectEntry] = {}
    common: set[str] = set()
    for entry in objects:
        if not entry.key.startswith(prefix):
            continue
        rest = entry.key[len(prefix) :]
        if delimiter and (cut := rest.find(delimiter)) != -1:
            common.add(prefix + rest[: cut + len(delimiter)])
        else:
            contents[entry.key] = entry
    return contents, common


def _paginate(
    contents: dict[str, S3ObjectEntry], prefixes: set[str], req: ListRequest
) -> ListResult:
    """Order objects and prefixes by key, resume after the cursor, truncate."""
    items: list[tuple[str, S3ObjectEntry | None]] = [(e.key, e) for e in contents.values()]
    items.extend((cp, None) for cp in prefixes)
    items.sort(key=lambda item: item[0])

    after = _resume_after(req)
    if after is not None:
        items = [item for item in items if item[0] > after]

    limit = max(0, min(req.max_keys, MAX_KEYS))
    page = items[:limit]
    is_truncated = len(items) > limit
    next_token = encode_token(page[-1][0]) if is_truncated and page else None

    return ListResult(
        entries=tuple(entry for _, entry in page if entry is not None),
        common_prefixes=tuple(key for key, entry in page if entry is None),
        is_truncated=is_truncated,
        next_token=next_token,
        prefix=req.prefix,
        delimiter=req.delimiter,
    )


def _resume_after(req: ListRequest) -> str | None:
    """The key a page resumes strictly after -- the token wins over start-after."""
    if req.continuation_token is not None:
        return decode_token(req.continuation_token)
    return req.start_after
