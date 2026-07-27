"""HTTP Basic authentication for the WebDAV surface.

The credential is an existing S3 access key: the key id is the username and its
secret is the password. Nothing new is issued, so a key's revocation, sealing and
audit trail already cover this surface -- see `authentication/spec.md`, "S3 access
keys are a credential, not an identity".

WebDAV clients overwhelmingly speak Basic, and several cannot send an arbitrary
`Authorization` header at all. Accepting bearer tokens here as well would mean two
authentication paths to audit and only one that clients use, so a bearer token is
refused rather than quietly supported.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from datetime import datetime

from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import AuthenticationError
from cyberfs.domain.ports.crypto import KeyProvider
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

#: Sealed once at construction so an unknown or revoked key id can be "unsealed"
#: at the same AES-GCM cost a real one incurs. Without it the response time
#: separates "no such key" from "wrong secret", which turns key ids into an
#: enumerable space. Copied deliberately from `s3_auth.py` rather than reinvented.
_DUMMY_SECRET = "cyberfs/unknown-webdav-key/constant-time-placeholder-secret"  # noqa: S105


class WebDavAuthError(AuthenticationError):
    """The credential was absent, malformed, unknown, wrong, or revoked.

    Deliberately one error for all of those: a client learns that it failed, not
    which way it failed.
    """

    code = "webdav_unauthorized"
    title = "WebDAV authentication failed"


class WebDavAuthenticator:
    def __init__(self, keys: KeyProvider) -> None:
        self._keys = keys
        self._dummy_sealed = keys.seal_secret(_DUMMY_SECRET.encode("utf-8"))
        self._dummy_master_key_id = keys.master_key_id

    async def authenticate(
        self, uow: UnitOfWork, header: str | None, *, now: datetime | None = None
    ) -> S3AccessKey:
        """Resolve a Basic header to the active access key it names."""
        moment = now or utcnow()
        credentials = _parse_basic(header)
        if credentials is None:
            raise WebDavAuthError("basic credentials are required")
        key_id, secret = credentials

        key = await uow.s3_keys.get_by_key_id(key_id)
        if key is None or not key.is_active:
            # Burn the same unseal and comparison against a placeholder, so an
            # unknown or revoked key costs what a real one with a wrong secret does.
            expected = self._unseal(self._dummy_sealed, self._dummy_master_key_id)
            hmac.compare_digest(expected, secret)
            raise WebDavAuthError("credentials are not recognised")

        expected = self._unseal(key.sealed_secret, key.secret_master_key_id)
        if not hmac.compare_digest(expected, secret):
            raise WebDavAuthError("credentials are not recognised")

        await uow.s3_keys.update(key.with_last_used(moment))
        logger.info("webdav_authenticated", key_id=key.key_id, subject=key.owner_subject)
        return key

    def _unseal(self, sealed: bytes, master_key_id: str) -> str:
        return self._keys.unseal_secret(sealed, master_key_id=master_key_id).decode("utf-8")


def _parse_basic(header: str | None) -> tuple[str, str] | None:
    """Split a Basic header into its key id and secret, or None if it is not one.

    A bearer token lands here and returns None, which is how it comes to be
    refused: this surface takes access keys and nothing else.
    """
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    key_id, separator, secret = decoded.partition(":")
    if not separator or not key_id:
        return None
    return key_id, secret
