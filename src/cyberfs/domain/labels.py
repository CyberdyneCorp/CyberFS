"""Partial label updates: a delta, and the collection it produces.

Apart from `nodes.py` because a delta is a different kind of thing from a
collection. It is validated in both directions, it can contradict itself, and
its limits apply to what it *results in* rather than to what it names -- a
request adding one tag to a node already at the maximum is refused even though
the request itself is tiny.

The constants and the per-entry rules stay where they are; nothing here relaxes
them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from cyberfs.domain.errors import ValidationError
from cyberfs.domain.nodes import (
    MAX_METADATA_PAIRS,
    RESERVED_METADATA_PREFIX,
    validate_metadata,
    validate_tags,
)


@dataclass(frozen=True, slots=True)
class TagDelta:
    """A change to a node's tags, both directions already normalized."""

    added: frozenset[str]
    removed: frozenset[str]


@dataclass(frozen=True, slots=True)
class MetadataDelta:
    """A change to a node's metadata: pairs to write, keys to drop."""

    pairs: Mapping[str, str]
    removed: frozenset[str]


def validate_tag_delta(add: Iterable[str], remove: Iterable[str]) -> TagDelta:
    """Normalize and check both directions of a tag change.

    A removal normalizes exactly as a write does, because the normalized form is
    the only form stored: `remove: ["URGENT"]` can mean nothing but `urgent`.
    Each side is bounded by the per-node maximum, since no legitimate request
    names more tags than a node could hold.
    """
    added = validate_tags(add)
    removed = validate_tags(remove)
    if not added and not removed:
        raise ValidationError("a tag update must add or remove at least one tag")
    contradictory = added & removed
    if contradictory:
        # Neither "add wins" nor "remove wins": either is a coin flip the caller
        # did not ask us to make, and a request naming both is far more likely a
        # bug than an intent.
        raise ValidationError(
            f"tag {sorted(contradictory)[0]!r} is named as both an addition and a removal"
        )
    return TagDelta(added=added, removed=removed)


def validate_metadata_delta(
    pairs: Sequence[tuple[str, str]], remove: Iterable[str]
) -> MetadataDelta:
    """Check the pairs to write and the keys to drop.

    Keys are matched byte for byte in both directions, as they are everywhere
    else -- unlike a tag, a metadata key is what the integration wrote.
    """
    written = validate_metadata(pairs)
    removed = _validate_removal_keys(remove)
    if not written and not removed:
        raise ValidationError("a metadata update must set or remove at least one key")
    contradictory = set(written) & removed
    if contradictory:
        raise ValidationError(
            f"metadata key {sorted(contradictory)[0]!r} is named as both a set and a removal"
        )
    return MetadataDelta(pairs=written, removed=removed)


def merge_tags(current: frozenset[str], delta: TagDelta) -> frozenset[str]:
    """The tag set the node will carry once `delta` is applied.

    Re-validated rather than merely counted, so the limit a merge trips over is
    reported with the same message a replace gives. Every entry is already in
    its stored form, so the normalization pass is a no-op.
    """
    return validate_tags((current | delta.added) - delta.removed)


def merge_metadata(current: Mapping[str, str], delta: MetadataDelta) -> dict[str, str]:
    """The pairs the node will carry once `delta` is applied.

    Counted rather than re-validated: a pair CyberFS wrote in the reserved
    namespace is legitimately on the node, and `validate_metadata` would refuse
    it. It still occupies a row, so it still counts towards the maximum.
    """
    merged = {key: value for key, value in current.items() if key not in delta.removed}
    merged.update(delta.pairs)
    if len(merged) > MAX_METADATA_PAIRS:
        raise ValidationError(f"a node may carry at most {MAX_METADATA_PAIRS} metadata pairs")
    return merged


def is_reserved_key(key: str) -> bool:
    """Whether `key` lies in the namespace CyberFS keeps for itself.

    Casefolded, exactly as `validate_metadata` tests it on the way in. Every test
    of the prefix in this change goes through here, because a predicate that
    disagreed with the write-side guard would be a hole in the namespace: a key
    a caller cannot write but can remove, or cannot write but is shown.
    """
    return key.casefold().startswith(RESERVED_METADATA_PREFIX)


def visible_metadata(pairs: Mapping[str, str]) -> dict[str, str]:
    """The pairs a caller may see, which are exactly the ones it may write back.

    A reserved pair survives every write a caller makes, so leaving it in the
    response would hand a client a key it can neither change nor delete -- and one
    that makes the object it was just given fail validation if it replaces it
    unchanged. The repository read stays unfiltered: that is how CyberFS reads its
    own namespace, and how backup carries it.
    """
    return {key: value for key, value in pairs.items() if not is_reserved_key(key)}


def tag_change_counts(current: frozenset[str], merged: frozenset[str]) -> tuple[int, int]:
    """How many tags the merge adds and how many it drops.

    Counts, never the tags themselves: the audit store is pruned on a different
    clock from the labels, so copying label text into it would outlive the label.
    """
    return len(merged - current), len(current - merged)


def metadata_change_counts(
    current: Mapping[str, str], merged: Mapping[str, str]
) -> tuple[int, int]:
    """How many pairs the merge writes and how many keys it drops.

    A pair whose value is unchanged is not counted as written -- it is not a
    change.
    """
    written = sum(1 for key, value in merged.items() if current.get(key) != value)
    return written, len(set(current) - set(merged))


def _validate_removal_keys(keys: Iterable[str]) -> frozenset[str]:
    """Keys named for deletion, checked as strictly as keys named for writing.

    Borrowed from `validate_metadata` by pairing each key with an empty value:
    every rule that applies to a key -- length, appearing once, and above all the
    reserved namespace -- applies identically when the key is being deleted, and
    the value slot is unused. Refusing a reserved key here is what makes the
    namespace trustworthy: a caller who could delete `cyberfs.*` could empty
    metadata CyberFS wrote about their own node.
    """
    return frozenset(validate_metadata([(key, "") for key in keys]))
