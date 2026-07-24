"""AWS Signature Version 4 -- the pure algorithm.

Every function here is a single, side-effect-free step of the published SigV4
signing process, so each is testable against AWS's own vectors and none needs
Postgres, a clock, or a request object present. The layering guard forbids the
`cryptography` package in a pure layer; the standard library's `hashlib`/`hmac`
are stdlib, not that package, and are the correct primitives for HMAC-SHA256,
so they are used directly.

The verifier (`application/s3_auth.py`) composes these to reproduce exactly
what a client signed and compares the two signatures in constant time. Because
verification *reproduces* the client's canonical request, the signed-header
list is used in the order the client presented it rather than re-sorted -- a
client that failed to sort simply fails to match, which is the correct outcome.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote

from cyberfs.domain.errors import SignatureMismatchError

#: The only algorithm CyberFS accepts on the S3 surface.
ALGORITHM = "AWS4-HMAC-SHA256"
#: The scope terminator every SigV4 credential ends with.
TERMINATOR = "aws4_request"
#: A payload the client chose not to hash -- valid per the S3 spec, signed as a
#: literal instead of a digest, so the body cannot be pinned by the signature.
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
#: SHA-256 of the empty string; the payload hash of a body-less request.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

#: The wire format AWS stamps into `x-amz-date`.
AMZ_DATE_FORMAT = "%Y%m%dT%H%M%SZ"


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256, the digest SigV4 hashes payloads and canonical requests to."""
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def canonical_uri(path: str) -> str:
    """The path, URI-encoded once, unreserved characters left intact.

    Empty becomes `/`. `/` is preserved as the segment separator; everything
    outside the RFC 3986 unreserved set is percent-encoded. CyberFS follows the
    S3 rule of a single encoding pass (S3 does not normalise the path).
    """
    if not path:
        return "/"
    return quote(path, safe="/-_.~")


def canonical_query(query: str) -> str:
    """Query parameters sorted by name, each key and value percent-encoded.

    An empty query is the empty string. Blank values are kept, so a bare flag
    parameter still appears as `flag=`.
    """
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    encoded = sorted((quote(k, safe=""), quote(v, safe="")) for k, v in pairs)
    return "&".join(f"{k}={v}" for k, v in encoded)


def _trim_value(value: str) -> str:
    """Collapse internal whitespace runs and strip, as SigV4 requires."""
    return " ".join(value.split())


def canonical_headers(headers: Mapping[str, str], signed_headers: Sequence[str]) -> str:
    """The signed headers as `name:value\\n` lines, names lowercased.

    Header lookup is case-insensitive; a signed header absent from the request
    contributes an empty value, so the reproduced canonical request diverges
    (and the signature fails to match) rather than silently dropping the line.
    """
    lookup = {name.lower(): value for name, value in headers.items()}
    return "".join(f"{name}:{_trim_value(lookup.get(name, ''))}\n" for name in signed_headers)


def signed_headers_line(signed_headers: Sequence[str]) -> str:
    """The `;`-joined signed-header list, as it appears in the canonical request."""
    return ";".join(signed_headers)


def canonical_request(
    method: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    signed_headers: Sequence[str],
    payload_hash: str,
) -> str:
    """Assemble the canonical request from its six documented components."""
    return "\n".join(
        (
            method,
            canonical_uri(path),
            canonical_query(query),
            canonical_headers(headers, signed_headers),
            signed_headers_line(signed_headers),
            payload_hash,
        )
    )


def credential_scope(date_stamp: str, region: str, service: str) -> str:
    """`date/region/service/aws4_request` -- the scope a signature is bound to."""
    return f"{date_stamp}/{region}/{service}/{TERMINATOR}"


def string_to_sign(amz_date: str, scope: str, request: str) -> str:
    """The string-to-sign: algorithm, timestamp, scope, and the request digest."""
    return "\n".join((ALGORITHM, amz_date, scope, sha256_hex(request.encode("utf-8"))))


def signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    """Derive the signing key by the documented HMAC chain over the scope."""
    k_date = _hmac(f"AWS4{secret}".encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, TERMINATOR)


def signature(key: bytes, to_sign: str) -> str:
    """The final hex signature: HMAC-SHA256 of the string-to-sign under the key."""
    return hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(computed: str, presented: str) -> bool:
    """Constant-time signature comparison.

    `hmac.compare_digest` takes time independent of how many leading bytes
    match, so a caller cannot narrow a forged signature byte by byte from
    response timing.
    """
    return hmac.compare_digest(computed, presented)


def parse_amz_date(value: str) -> datetime:
    """Parse a signed `x-amz-date` (`YYYYMMDDThhmmssZ`) as an aware UTC instant.

    A value that does not parse is a malformed request, not a skew failure, so
    it surfaces as `SignatureDoesNotMatch` alongside every other unparseable
    signing input.
    """
    try:
        return datetime.strptime(value, AMZ_DATE_FORMAT).replace(tzinfo=UTC)
    except (ValueError, TypeError) as exc:
        raise SignatureMismatchError("x-amz-date is malformed") from exc
