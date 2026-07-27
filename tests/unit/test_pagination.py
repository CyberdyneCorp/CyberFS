"""Keyset cursors: what round-trips, and what is refused.

This module is the shared foundation under every paginated surface, so the
properties worth pinning are the ones a caller can violate from outside: a
truncated token, a token from a different walk, and a separator appearing inside
a value that has to survive the trip.
"""

from __future__ import annotations

import pytest

from cyberfs.domain.errors import ValidationError
from cyberfs.domain.pagination import (
    CURSOR_SEPARATOR,
    decode_cursor,
    decode_keyed_cursor,
    encode_cursor,
    encode_keyed_cursor,
    fingerprint_of,
)

# --- the opaque cursor -----------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["", "simple", "a b/c", "2026-07-27T12:00:00+00:00", "emoji-🔐", "x" * 500],
)
def test_an_opaque_cursor_round_trips(value: str) -> None:
    assert decode_cursor(encode_cursor(value)) == value


def test_an_opaque_cursor_carries_no_padding() -> None:
    """It travels in a query string, where `=` needs escaping for no benefit."""
    assert "=" not in encode_cursor("something-that-would-pad")


def test_a_mangled_opaque_cursor_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValidationError, match="cursor is not valid"):
        decode_cursor("!!!not-base64!!!")


# --- the keyed cursor ------------------------------------------------------


def test_a_keyed_cursor_round_trips_its_fields() -> None:
    print_ = fingerprint_of("term", ["tag"])
    raw = encode_keyed_cursor(print_, "some-id", "a name")
    assert decode_keyed_cursor(raw, fingerprint=print_, fields=2) == ("some-id", "a name")


def test_the_last_field_may_contain_the_separator() -> None:
    """A node name is variable-width and goes last, so it may hold anything.

    Splitting with a bounded count is what allows that; a plain `split` would
    turn a name containing the separator into two fields and shift the payload.
    """
    print_ = fingerprint_of(None)
    awkward = f"name{CURSOR_SEPARATOR}with{CURSOR_SEPARATOR}separators"
    raw = encode_keyed_cursor(print_, "id-1", awkward)
    assert decode_keyed_cursor(raw, fingerprint=print_, fields=2) == ("id-1", awkward)


def test_a_truncated_keyed_cursor_is_refused() -> None:
    """The reason the check digest exists: without it this resumes from a
    position nobody chose, and the caller silently loses a prefix of the walk."""
    print_ = fingerprint_of("term")
    raw = encode_keyed_cursor(print_, "id-1", "name")
    with pytest.raises(ValidationError, match="cursor is not valid"):
        decode_keyed_cursor(raw[:-4], fingerprint=print_, fields=2)


def test_a_cursor_from_a_different_walk_is_refused_distinctly() -> None:
    """A different message from a mangled one: this is a real cursor being
    presented against the wrong result set, which is a caller bug, not corruption."""
    issued = encode_keyed_cursor(fingerprint_of("first"), "id-1")
    with pytest.raises(ValidationError, match="different filter set"):
        decode_keyed_cursor(issued, fingerprint=fingerprint_of("second"), fields=1)


def test_a_cursor_with_the_wrong_field_count_is_refused() -> None:
    print_ = fingerprint_of(None)
    raw = encode_keyed_cursor(print_, "only-one")
    with pytest.raises(ValidationError, match="cursor is not valid"):
        decode_keyed_cursor(raw, fingerprint=print_, fields=2)


@pytest.mark.parametrize("raw", ["", CURSOR_SEPARATOR, "no-separator-at-all"])
def test_a_payload_that_was_never_issued_is_refused(raw: str) -> None:
    with pytest.raises(ValidationError, match="cursor is not valid"):
        decode_keyed_cursor(raw, fingerprint=fingerprint_of(None), fields=1)


# --- the fingerprint -------------------------------------------------------


def test_a_separator_inside_a_value_cannot_forge_a_match() -> None:
    """Why the fingerprint is JSON and not a joined string.

    Joining on a delimiter makes `["a,b"]` and `["a", "b"]` hash alike, so two
    genuinely different filter sets would share a cursor space.
    """
    assert fingerprint_of("a,b") != fingerprint_of("a", "b")
    assert fingerprint_of(["a,b"]) != fingerprint_of(["a", "b"])


def test_the_fingerprint_distinguishes_absent_from_empty() -> None:
    assert fingerprint_of(None) != fingerprint_of("")


def test_the_fingerprint_is_stable_across_calls() -> None:
    """A cursor issued by one process is read by another, so it cannot depend on
    anything but its inputs -- no salt, no hash randomization."""
    assert fingerprint_of("term", ["a", "b"]) == fingerprint_of("term", ["a", "b"])


def test_the_fingerprint_is_order_sensitive_so_callers_must_normalize() -> None:
    """Documented rather than papered over: sorting happens in the filter value
    object, not here, because only the caller knows which lists are sets."""
    assert fingerprint_of(["a", "b"]) != fingerprint_of(["b", "a"])
