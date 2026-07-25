"""Issuing a presigned URL that resolves to CyberFS.

A presigned URL lets a client perform one S3 operation with a signature carried
in the query string rather than a live credential. CyberFS's rule is strict:
every URL it issues addresses *its own* S3 endpoint and is signed with a CyberFS
access-key secret, so following it makes the bytes transit CyberFS subject to
authorization, quota, and decryption. CyberFS never issues a URL that grants
direct access to the underlying object store, nor one carrying an object-store
credential (`s3-compatibility/spec.md`, "Presigned URLs resolve to CyberFS";
`file-storage/spec.md`, "File download").

The secret is recovered from the sealed key through the `KeyProvider` port, used
to sign, and never returned; the pure query construction lives in
:mod:`cyberfs.domain.s3.presigned`.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import InvalidArgumentError
from cyberfs.domain.ports.crypto import KeyProvider
from cyberfs.domain.s3 import presigned, sigv4
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)


class S3PresignService:
    """Signs presigned URLs against CyberFS's own S3 endpoint.

    `endpoint` is the externally visible CyberFS S3 base URL (scheme + host);
    the service refuses to construct without one, so a URL is never minted
    without a CyberFS endpoint to root it at.
    """

    def __init__(
        self,
        keys: KeyProvider,
        *,
        endpoint: str,
        base_path: str,
        region: str,
        service: str = "s3",
    ) -> None:
        if not endpoint:
            raise ValueError("a CyberFS S3 endpoint is required to issue a presigned URL")
        self._keys = keys
        self._endpoint = endpoint.rstrip("/")
        self._base_path = base_path
        self._region = region
        self._service = service
        self._host = urlsplit(self._endpoint).netloc

    def presign(
        self,
        key: S3AccessKey,
        method: str,
        bucket: str,
        object_key: str,
        *,
        expires: int,
        now: datetime | None = None,
    ) -> str:
        """A presigned URL for one operation on `bucket`/`object_key`.

        The URL is valid for `expires` seconds from `now` and is bound to `key`
        -- revoking the key stops it working on the very next request, since the
        verifier reloads the key and refuses a revoked one.
        """
        if expires <= 0 or expires > presigned.MAX_PRESIGN_EXPIRES:
            raise InvalidArgumentError("presigned URL validity exceeds the maximum")
        moment = now or utcnow()
        secret = self._keys.unseal_secret(
            key.sealed_secret, master_key_id=key.secret_master_key_id
        ).decode("utf-8")
        path = presigned.object_path(self._base_path, bucket, object_key)
        query = presigned.build_presigned_query(
            method=method,
            path=path,
            host=self._host,
            access_key_id=key.key_id,
            secret=secret,
            region=self._region,
            service=self._service,
            amz_date=moment.strftime(sigv4.AMZ_DATE_FORMAT),
            expires=expires,
        )
        url = presigned.presigned_url(self._endpoint, path, query)
        logger.info("s3_presigned_url_issued", key_id=key.key_id, method=method, expires=expires)
        return url
