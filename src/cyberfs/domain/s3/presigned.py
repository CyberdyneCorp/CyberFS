"""Presigned URL SigV4 -- the query-parameter form, pure.

A presigned URL carries the whole signature in the query string rather than an
``Authorization`` header: ``X-Amz-Algorithm``, ``X-Amz-Credential``,
``X-Amz-Date``, ``X-Amz-Expires``, ``X-Amz-SignedHeaders``, and
``X-Amz-Signature``. The payload is signed as the literal ``UNSIGNED-PAYLOAD``,
so the URL can be handed to a client that streams an arbitrary body.

This module composes the primitives in :mod:`cyberfs.domain.s3.sigv4`; it stays
pure -- no clock, no repository, no I/O. Two rules distinguish the query form
from the header form and are the reason it lives here rather than being folded
into ``sigv4``:

* the canonical query signed over is the request query with ``X-Amz-Signature``
  removed (a signature cannot sign itself), every other parameter kept,
  sorted, and percent-encoded; and
* the payload hash is always ``UNSIGNED-PAYLOAD``.

Generation (:func:`build_presigned_query`, :func:`presigned_url`) always roots
the URL at CyberFS's *own* S3 endpoint and binds it to a CyberFS access key --
never the object store, never an object-store credential
(`s3-compatibility/spec.md`, "Presigned URLs resolve to CyberFS").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode

from cyberfs.domain.s3 import sigv4
from cyberfs.domain.s3.sigv4 import ALGORITHM, TERMINATOR, UNSIGNED_PAYLOAD

#: The one query parameter excluded from the canonical query, because it *is*
#: the signature and so cannot be part of what was signed.
SIGNATURE_PARAM = "X-Amz-Signature"

#: A credential scope is exactly ``key/date/region/service/aws4_request``.
_SCOPE_PARTS = 5

#: The header a presigned URL always signs, so the URL is bound to the host it
#: addresses and cannot be replayed against a different endpoint.
_DEFAULT_SIGNED_HEADERS = ("host",)


@dataclass(frozen=True, slots=True)
class PresignedQuery:
    """The parsed ``X-Amz-*`` parameters of a presigned request."""

    access_key_id: str
    date_stamp: str
    region: str
    service: str
    amz_date: str
    expires: int
    signed_headers: tuple[str, ...]
    signature: str


def _parse_scope(credential: str) -> tuple[str, str, str, str] | None:
    """Split a credential scope, verifying its arity and terminator."""
    parts = credential.split("/")
    if len(parts) != _SCOPE_PARTS or parts[4] != TERMINATOR:
        return None
    access_key_id, date_stamp, region, service, _terminator = parts
    if not all((access_key_id, date_stamp, region, service)):
        return None
    return access_key_id, date_stamp, region, service


def parse_presigned_query(query: str) -> PresignedQuery | None:
    """Parse the ``X-Amz-*`` presign parameters, or ``None`` if malformed.

    Total and defensive, exactly as the header parser is: any missing part, a
    wrong algorithm token, a credential of the wrong arity, or a non-numeric
    expiry maps to ``None``, so the verifier refuses every broken shape with the
    same ``SignatureDoesNotMatch`` rather than disclosing which part was wrong.
    """
    if not query:
        return None
    lower = {key.lower(): value for key, value in parse_qsl(query, keep_blank_values=True)}
    if lower.get("x-amz-algorithm") != ALGORITHM:
        return None
    credential = lower.get("x-amz-credential")
    amz_date = lower.get("x-amz-date")
    expires_raw = lower.get("x-amz-expires")
    signed = lower.get("x-amz-signedheaders")
    signature = lower.get("x-amz-signature")
    if not credential or not amz_date or not expires_raw or not signed or not signature:
        return None
    scope = _parse_scope(credential)
    if scope is None:
        return None
    expires = _parse_expires(expires_raw)
    if expires is None:
        return None
    signed_headers = tuple(header for header in signed.split(";") if header)
    if not signed_headers:
        return None
    access_key_id, date_stamp, region, service = scope
    return PresignedQuery(
        access_key_id=access_key_id,
        date_stamp=date_stamp,
        region=region,
        service=service,
        amz_date=amz_date,
        expires=expires,
        signed_headers=signed_headers,
        signature=signature,
    )


def _parse_expires(raw: str) -> int | None:
    try:
        expires = int(raw)
    except ValueError:
        return None
    return expires if expires > 0 else None


def canonical_query_without_signature(query: str) -> str:
    """The canonical query for a presigned request: every parameter except the
    signature, percent-encoded and sorted by name, as SigV4 requires."""
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [(key, value) for key, value in pairs if key.lower() != SIGNATURE_PARAM.lower()]
    encoded = sorted((quote(key, safe=""), quote(value, safe="")) for key, value in kept)
    return "&".join(f"{key}={value}" for key, value in encoded)


def presigned_canonical_request(
    method: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    signed_headers: tuple[str, ...],
) -> str:
    """Assemble the canonical request a presigned URL is signed over.

    Identical to the header form but for two rules: the canonical query omits
    ``X-Amz-Signature``, and the payload hash is the literal ``UNSIGNED-PAYLOAD``.
    """
    return "\n".join(
        (
            method,
            sigv4.canonical_uri(path),
            canonical_query_without_signature(query),
            sigv4.canonical_headers(headers, signed_headers),
            sigv4.signed_headers_line(signed_headers),
            UNSIGNED_PAYLOAD,
        )
    )


def object_path(base_path: str, bucket: str, key: str) -> str:
    """The URL path of an S3 object: ``{base_path}/{bucket}/{key}``.

    This is what the verifier reads from ``request.url.path`` and re-encodes, so
    generation must sign over exactly the same value.
    """
    return f"{base_path.rstrip('/')}/{bucket}/{key}"


def build_presigned_query(
    *,
    method: str,
    path: str,
    host: str,
    access_key_id: str,
    secret: str,
    region: str,
    service: str,
    amz_date: str,
    expires: int,
    signed_headers: tuple[str, ...] = _DEFAULT_SIGNED_HEADERS,
) -> str:
    """Build the full presigned query string, signature appended.

    Assembles the five signed ``X-Amz-*`` parameters, signs the canonical
    request they and `path` form, and appends ``X-Amz-Signature``. The result
    references neither the object store nor any object-store credential.
    """
    date_stamp = amz_date[:8]
    scope = sigv4.credential_scope(date_stamp, region, service)
    signed_params = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": f"{access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": ";".join(signed_headers),
    }
    unsigned_query = urlencode(signed_params)
    request_string = presigned_canonical_request(
        method, path, unsigned_query, {"host": host}, signed_headers
    )
    to_sign = sigv4.string_to_sign(amz_date, scope, request_string)
    key = sigv4.signing_key(secret, date_stamp, region, service)
    signature = sigv4.signature(key, to_sign)
    return f"{unsigned_query}&{urlencode({SIGNATURE_PARAM: signature})}"


def presigned_url(endpoint: str, path: str, query: str) -> str:
    """The presigned URL: CyberFS's own endpoint, the object path, the query.

    `endpoint` is CyberFS's S3 base URL (scheme + host, no trailing slash); the
    URL it yields carries a CyberFS access-key credential and points at CyberFS,
    so following it makes the bytes transit CyberFS under the usual checks.
    """
    return f"{endpoint.rstrip('/')}{sigv4.canonical_uri(path)}?{query}"
