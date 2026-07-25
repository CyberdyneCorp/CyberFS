"""Parsing the S3 `Authorization` header.

A SigV4 `Authorization` header packs the algorithm, the credential scope, the
signed-header list, and the signature into one line:

    AWS4-HMAC-SHA256 Credential=AKID/20150830/us-east-1/service/aws4_request,\
 SignedHeaders=host;x-amz-date, Signature=<hex>

Parsing is total and defensive: any malformed or truncated header -- a wrong
algorithm token, a missing part, a credential scope of the wrong arity -- maps
to `None`, so the verifier can refuse every shape of bad header with the same
`SignatureDoesNotMatch` rather than leaking which part was wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from cyberfs.domain.s3.sigv4 import ALGORITHM, TERMINATOR

#: A credential scope is exactly `key/date/region/service/aws4_request`.
_SCOPE_PARTS = 5


@dataclass(frozen=True, slots=True)
class AuthorizationHeader:
    """The parsed pieces of a SigV4 `Authorization` header."""

    access_key_id: str
    date_stamp: str
    region: str
    service: str
    signed_headers: tuple[str, ...]
    signature: str


def _parse_pairs(remainder: str) -> dict[str, str] | None:
    """Split the `key=value, key=value` tail into a mapping, or `None`."""
    pairs: dict[str, str] = {}
    for segment in remainder.split(","):
        key, sep, value = segment.strip().partition("=")
        if not sep or not key:
            return None
        pairs[key.strip()] = value.strip()
    return pairs


def _parse_scope(credential: str) -> tuple[str, str, str, str] | None:
    """Split a credential scope, verifying its arity and terminator."""
    parts = credential.split("/")
    if len(parts) != _SCOPE_PARTS or parts[4] != TERMINATOR:
        return None
    access_key_id, date_stamp, region, service, _terminator = parts
    if not all((access_key_id, date_stamp, region, service)):
        return None
    return access_key_id, date_stamp, region, service


def parse_authorization_header(value: str | None) -> AuthorizationHeader | None:
    """Parse a SigV4 `Authorization` header, or `None` if it is malformed."""
    prefix = f"{ALGORITHM} "
    if not value or not value.startswith(prefix):
        return None
    pairs = _parse_pairs(value[len(prefix) :])
    if pairs is None:
        return None
    credential = pairs.get("Credential")
    signed = pairs.get("SignedHeaders")
    presented = pairs.get("Signature")
    if not credential or not signed or not presented:
        return None
    scope = _parse_scope(credential)
    if scope is None:
        return None
    signed_headers = tuple(header for header in signed.split(";") if header)
    if not signed_headers:
        return None
    access_key_id, date_stamp, region, service = scope
    return AuthorizationHeader(
        access_key_id=access_key_id,
        date_stamp=date_stamp,
        region=region,
        service=service,
        signed_headers=signed_headers,
        signature=presented,
    )
