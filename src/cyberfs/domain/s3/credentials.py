"""Classifying the credential an S3 request carries.

The S3 surface accepts two credentials -- an AWS SigV4 signature and a
CyberdyneAuth bearer token -- but never both at once: a request presenting both
is refused with ``400`` rather than silently resolved to one
(``s3-compatibility/spec.md``, "Both credentials on one request is refused").
This module is the pure classifier that decision rests on; the application-layer
resolver acts on its verdict.

A SigV4 credential can arrive two ways: in the ``Authorization`` header
(``AWS4-HMAC-SHA256 …``) or, for a presigned URL, as an ``X-Amz-Signature``
query parameter. Either counts.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from cyberfs.domain.s3.sigv4 import ALGORITHM

_BEARER_PREFIX = "Bearer "


class S3Credential(StrEnum):
    """The credential that established an authenticated S3 caller."""

    SIGV4 = "sigv4"
    BEARER = "bearer"


class S3CredentialForm(StrEnum):
    """Which credential(s) an incoming request presents."""

    SIGV4 = "sigv4"
    BEARER = "bearer"
    BOTH = "both"
    NONE = "none"


def classify_credentials(headers: Mapping[str, str], query: str) -> S3CredentialForm:
    """Report which credential(s) the request carries, without verifying any."""
    authorization = _authorization(headers)
    has_sigv4 = authorization.startswith(ALGORITHM) or "x-amz-signature=" in query.lower()
    has_bearer = authorization.startswith(_BEARER_PREFIX)
    if has_sigv4 and has_bearer:
        return S3CredentialForm.BOTH
    if has_sigv4:
        return S3CredentialForm.SIGV4
    if has_bearer:
        return S3CredentialForm.BEARER
    return S3CredentialForm.NONE


def bearer_token(headers: Mapping[str, str]) -> str:
    """The token from an ``Authorization: Bearer …`` header, prefix stripped."""
    return _authorization(headers).removeprefix(_BEARER_PREFIX).strip()


def _authorization(headers: Mapping[str, str]) -> str:
    """The ``Authorization`` header, looked up case-insensitively."""
    for name, value in headers.items():
        if name.lower() == "authorization":
            return value
    return ""
