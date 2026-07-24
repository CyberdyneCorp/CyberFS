"""The pure S3 namespace mapping.

Each user's tree is presented as a single bucket named for their CyberdyneAuth
subject; folder paths map to key prefixes. Nodes shared *with* the caller are
reachable under a reserved ``shared/<owner-subject>/…`` prefix inside that same
bucket -- see ``s3-compatibility/spec.md``, "Namespace mapping".

This module is the single source of truth for that grammar. It is pure: no
I/O, no repository. Resolving *which* owners are actually shared with a caller
is a listing concern the read path answers against the grant repository; here
we only translate an address to ``(owner_subject, path)`` and back.

Two deliberate refusals live here so bucket names never become a user
directory:

* addressing a bucket that is not the caller's own is a ``NoSuchBucket`` --
  identical to a bucket that never existed, so existence cannot be probed;
* ``shared`` is reserved at the root of every tree (enforced at folder-create
  in ``NodeService``) so a real folder cannot shadow the shared view.
"""

from __future__ import annotations

from dataclasses import dataclass

from cyberfs.domain.errors import NoSuchBucketError, ValidationError
from cyberfs.domain.nodes import normalize_name

#: The reserved first segment addressing nodes shared with the caller. Imported
#: by the ``NodeService`` folder-create guard so the two stay in lockstep.
SHARED_PREFIX = "shared"


@dataclass(frozen=True, slots=True)
class S3Target:
    """A resolved S3 key: whose tree it lives in and where.

    ``path`` carries a leading slash and no trailing one, matching
    ``NodePath.path`` so the read path can compare it directly. ``via_shared``
    records that the address arrived through the reserved prefix, so a writer
    can be held to the grant it holds on another owner's subtree.
    """

    owner_subject: str
    path: str
    via_shared: bool


def bucket_for_subject(subject: str) -> str:
    """The bucket name a subject's tree is presented as. Intentionally 1:1."""
    return subject


def subject_for_bucket(bucket: str) -> str:
    """The subject a bucket names. The inverse of `bucket_for_subject`."""
    return bucket


def resolve_key(caller_subject: str, bucket: str, key: str) -> S3Target:
    """Resolve a bucket and key the caller addressed to a tree and a path.

    Raises `NoSuchBucketError` for any bucket that is not the caller's own --
    identically for every foreign subject, so existence cannot be inferred --
    and `ValidationError` for a malformed shared address.
    """
    if bucket != caller_subject:
        raise NoSuchBucketError("no such bucket", bucket=bucket)
    segments = _segments(key)
    if segments and segments[0] == SHARED_PREFIX:
        return _resolve_shared(caller_subject, segments)
    return S3Target(owner_subject=caller_subject, path=_path(segments), via_shared=False)


def key_for(caller_subject: str, owner_subject: str, path: str) -> str:
    """Render the key a caller uses to reach a node at `path` in `owner`'s tree.

    The inverse of `resolve_key`: a node in the caller's own tree maps to the
    bare path; a node owned by someone else is re-prefixed with
    ``shared/<owner-subject>/``.
    """
    tail = path.lstrip("/")
    if owner_subject == caller_subject:
        return tail
    return f"{SHARED_PREFIX}/{owner_subject}/{tail}".rstrip("/")


def _resolve_shared(caller_subject: str, segments: tuple[str, ...]) -> S3Target:
    if len(segments) < 2:
        raise ValidationError("the shared prefix requires an owner subject")
    owner = segments[1]
    if owner == caller_subject:
        # Your own tree is addressed without the prefix; allowing both spellings
        # would make one node reachable under two keys.
        raise ValidationError("your own tree is addressed without the shared prefix")
    return S3Target(owner_subject=owner, path=_path(segments[2:]), via_shared=True)


def _segments(key: str) -> tuple[str, ...]:
    """Split a key into normalized, non-empty path segments.

    NFC-normalized per segment, exactly as `normalize_name` folds a node name,
    so an address and the node it names cannot disagree on spelling.
    """
    return tuple(normalize_name(part) for part in key.split("/") if part)


def _path(segments: tuple[str, ...]) -> str:
    """Render segments as a leading-slash path, matching `NodePath.path`."""
    return "/" + "/".join(segments)
