"""Keyset cursors: the payload, and what refuses one that was not issued here.

Every paginated surface in CyberFS walks by keyset rather than offset, so each
one hands the caller an opaque token naming the last row it returned. This module
is the single place those tokens are built and read.

Two levels are offered, because the surfaces genuinely differ:

`encode_cursor`/`decode_cursor` carry an opaque string and nothing else. They are
what the unfiltered walks use -- the user list, an audit sweep -- where the only
thing a cursor has to survive is base64.

`encode_keyed_cursor`/`decode_keyed_cursor` add a check digest and a fingerprint
of the filters the walk was issued for. A cursor names a position in one specific
ordered result set; presented alongside different filters that position describes
nothing, and serving it would return a page of a walk the caller never asked for
-- no error, no duplicate, just a silently missing prefix of the results. So the
filters travel with the cursor, and a mismatch is refused.

The fingerprint is computed here rather than in each repository or router so that
"the filters this cursor was issued for" and "the filters this request implies"
are folded by one piece of code. Two computations of the same thing that could
drift apart would be worse than none.

Neither digest is a signature and neither needs to be: anybody can compute one,
and a forged cursor reaches only what its holder could reach by issuing the query
themselves. What the check digest buys is that a *truncated* cursor is refused
instead of quietly resuming from a position nobody chose.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cyberfs.domain.errors import ValidationError

#: Separates the fields of a keyed cursor payload.
CURSOR_SEPARATOR = "\x1f"


def encode_cursor(value: str) -> str:
    """An opaque token carrying `value`. No integrity claim, by design."""
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> str:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("cursor is not valid") from exc


def encode_keyed_cursor(fingerprint: str, *fields: str) -> str:
    """Build a cursor payload: a check digest, the filter fingerprint, the key.

    Only the *last* field may contain the separator, so a variable-width value --
    a node name, a tag -- goes last and the fixed-width identifier ahead of it.
    """
    payload = CURSOR_SEPARATOR.join([fingerprint, *fields])
    return CURSOR_SEPARATOR.join([fingerprint_of(payload), payload])


def decode_keyed_cursor(raw: str, *, fingerprint: str, fields: int) -> tuple[str, ...]:
    """Read a cursor payload back, refusing anything the system did not issue.

    Two distinct refusals, because they mean different things to whoever reads
    the log: a payload that does not match its own check digest was mangled in
    transit or invented, while one that matches but names other filters is a real
    cursor being walked against the wrong result set.
    """
    check, _, payload = raw.partition(CURSOR_SEPARATOR)
    if not payload or check != fingerprint_of(payload):
        raise ValidationError("cursor is not valid")
    parts = payload.split(CURSOR_SEPARATOR, fields)
    if len(parts) != fields + 1:
        raise ValidationError("cursor is not valid")
    if parts[0] != fingerprint:
        raise ValidationError("cursor was issued for a different filter set")
    return tuple(parts[1:])


def fingerprint_of(*parts: str | list[str] | None) -> str:
    """A short, stable digest of a normalized filter set.

    JSON rather than a joined string, because a separator inside a search term
    must not be able to make two different filter sets hash alike. Truncated
    because it only has to *detect* a mismatch, never resist one.
    """
    payload = json.dumps(parts, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


__all__ = [
    "CURSOR_SEPARATOR",
    "decode_cursor",
    "decode_keyed_cursor",
    "encode_cursor",
    "encode_keyed_cursor",
    "fingerprint_of",
]
