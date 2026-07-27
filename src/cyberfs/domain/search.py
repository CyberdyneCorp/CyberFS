"""Search filters, normalized once, each carrying the fingerprint a cursor binds to.

The filters a cursor was issued for and the filters a request implies have to be
folded by one piece of code or they drift, so both go through the value objects
here and both hash through `domain.pagination.fingerprint_of`. Neither the
repository nor the router knows how a fingerprint is computed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from cyberfs.domain.errors import ValidationError
from cyberfs.domain.nodes import MAX_TAG_LENGTH, MAX_TAGS_PER_NODE, normalize_tag
from cyberfs.domain.pagination import fingerprint_of


class TagMatch(StrEnum):
    """How several tag filters combine with *each other*.

    Nothing else: a name substring and a metadata filter keep narrowing in
    either mode. "Does any-of also loosen the name match" is the ambiguity that
    would make this parameter a liability.
    """

    ALL = "all"
    ANY = "any"

    @classmethod
    def parse(cls, value: str | TagMatch) -> TagMatch:
        """Refuse an unrecognized spelling rather than defaulting to `ALL`.

        Defense in depth: the HTTP route declares this enum, so Pydantic refuses
        `tag_match=anyy` before any domain code runs. This guard is for the next
        caller — a CLI, a background job — that has a raw string in hand.
        """
        try:
            return cls(value)
        except ValueError as exc:
            modes = ", ".join(mode.value for mode in cls)
            raise ValidationError(f"tag match mode must be one of: {modes}") from exc


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """One search's filters, normalized, with a fingerprint over them."""

    term: str | None = None
    tags: tuple[str, ...] = ()
    match: TagMatch = TagMatch.ALL
    key: str | None = None
    value: str | None = None

    @classmethod
    def of(
        cls,
        *,
        term: str | None = None,
        tags: Iterable[str] = (),
        match: TagMatch = TagMatch.ALL,
        key: str | None = None,
        value: str | None = None,
    ) -> SearchFilters:
        """Normalize and validate what a caller supplied.

        At least one filter is required: an unfiltered search would be a listing
        of everything the caller can reach, which is what walking the tree is
        for -- and a cursor over it would be a way to page through all of it.
        """
        mode = TagMatch.parse(match)
        cleaned = (term or "").strip()
        # A set, so `tag=a&tag=a` is one filter; sorted, so the fingerprint does
        # not depend on the order the parameters happened to arrive in.
        normalized = sorted({normalize_tag(t) for t in tags if normalize_tag(t)})
        if len(normalized) > MAX_TAGS_PER_NODE:
            # Bounded in both modes, so the parameter's validity never depends
            # on the mode. In all-of mode naming more tags than a node may carry
            # cannot match at all, and running that scan would be work spent to
            # return nothing.
            raise ValidationError(f"a search may name at most {MAX_TAGS_PER_NODE} tags")
        # The write path bounds a stored tag at the same length, so a longer
        # filter cannot match any row -- it would compile into a comparison
        # against a column too narrow to hold it. Refused rather than scanned,
        # for the same reason as the count above.
        if any(len(tag) > MAX_TAG_LENGTH for tag in normalized):
            raise ValidationError(f"a tag filter may be at most {MAX_TAG_LENGTH} characters")
        if value is not None and key is None:
            raise ValidationError("a metadata value needs the key it belongs to")
        if not cleaned and not normalized and key is None:
            raise ValidationError("a search needs a name, a tag, or a metadata key")
        return cls(
            term=cleaned or None,
            tags=tuple(normalized),
            match=mode,
            key=key,
            value=value,
        )

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.term, self.match.value, self.key, self.value, list(self.tags))


@dataclass(frozen=True, slots=True)
class TagFilters:
    """The narrowing applied to a tag inventory, with its fingerprint."""

    prefix: str | None = None

    @classmethod
    def of(cls, prefix: str | None = None) -> TagFilters:
        # Matched against the normalized tag form, so the case and surrounding
        # whitespace of what a caller typed do not change what they are offered
        # -- consistent with how a tag filter already matches.
        normalized = normalize_tag(prefix) if prefix else ""
        return cls(prefix=normalized or None)

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.prefix)


@dataclass(frozen=True, slots=True)
class TagUsage:
    """A tag and how many of the caller's reachable nodes carry it.

    Per caller, never global: the count covers the nodes that caller owns or
    holds an active grant on, so two users legitimately disagree about the same
    tag. A tag with no carrier in scope has no row here at all, so the count is
    never zero.
    """

    tag: str
    count: int
