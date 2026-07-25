"""Key wrapping under the deployment master key.

The top of the envelope from `content-encryption/spec.md`: `MASTER_KEY` seals
each user's KEK. Content sealing -- the DEK layer and the framed AEAD -- lands
with the encryption capability; this is only what provisioning needs today.

AES-256-GCM with a random nonce per wrap. The nonce is stored alongside the
ciphertext because it must be recovered to unwrap, and a nonce is not secret;
reusing one under the same key would be, which is why each wrap draws a fresh
one.
"""

from __future__ import annotations

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cyberfs.domain.errors import KeyUnavailableError

KEY_BYTES = 32
NONCE_BYTES = 12
#: Binds a wrapped blob to its purpose, so a KEK-wrapping ciphertext can never
#: be replayed where a DEK-wrapping one is expected.
KEK_ASSOCIATED_DATA = b"cyberfs/kek/v1"
#: The layer below. Distinct associated data means a sealed DEK can never
#: be replayed where a sealed KEK is expected, or the reverse.
DEK_ASSOCIATED_DATA = b"cyberfs/dek/v1"
#: S3 access-key secrets. Distinct again, so a sealed secret can never be
#: replayed where a sealed KEK or DEK is expected.
S3_SECRET_ASSOCIATED_DATA = b"cyberfs/s3-secret/v1"


def master_key_id(key: bytes) -> str:
    """A short, stable, non-reversible label for a master key.

    Recorded on every wrapped KEK so a rotation can tell what it has already
    rewrapped and resume after an interruption. It is a digest, so publishing
    it in a row or a log reveals nothing about the key.
    """
    return hashlib.sha256(b"cyberfs/master-key-id/v1" + key).hexdigest()[:16]


class MasterKeyProvider:
    """Implements the `KeyProvider` port.

    Accepts a previous master key so both are usable while a rotation is in
    flight: new material is always sealed under the current key, while material
    still sealed under the previous one keeps opening.
    """

    def __init__(self, master_key: bytes, *, previous: bytes | None = None) -> None:
        if len(master_key) != KEY_BYTES:
            raise KeyUnavailableError("master key must be 32 bytes")
        if previous is not None and len(previous) != KEY_BYTES:
            raise KeyUnavailableError("previous master key must be 32 bytes")
        self._current = master_key
        self._previous = previous
        self._current_id = master_key_id(master_key)
        self._previous_id = master_key_id(previous) if previous is not None else None

    @property
    def master_key_id(self) -> str:
        return self._current_id

    @property
    def previous_master_key_id(self) -> str | None:
        return self._previous_id

    def generate_kek(self) -> bytes:
        """A fresh 256-bit KEK from the OS CSPRNG."""
        return os.urandom(KEY_BYTES)

    def wrap_kek(self, kek: bytes) -> bytes:
        if len(kek) != KEY_BYTES:
            raise KeyUnavailableError("key-encryption key must be 32 bytes")
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._current).encrypt(nonce, kek, KEK_ASSOCIATED_DATA)
        return nonce + sealed

    def unwrap_kek(self, wrapped: bytes, *, master_key_id: str) -> bytes:
        """Unseal a KEK using whichever master key sealed it."""
        key = self._key_for(master_key_id)
        nonce, sealed = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        try:
            return AESGCM(key).decrypt(nonce, sealed, KEK_ASSOCIATED_DATA)
        except InvalidTag as exc:
            # Deliberately opaque: never echo nonce, tag, or ciphertext.
            raise KeyUnavailableError("wrapped key failed authentication") from exc

    # --- data keys -----------------------------------------------------

    def wrap_dek(self, dek: bytes, kek: bytes) -> bytes:
        if len(dek) != KEY_BYTES:
            raise KeyUnavailableError("data key must be 32 bytes")
        nonce = os.urandom(NONCE_BYTES)
        return nonce + AESGCM(kek).encrypt(nonce, dek, DEK_ASSOCIATED_DATA)

    def unwrap_dek(self, wrapped: bytes, kek: bytes) -> bytes:
        nonce, sealed = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        try:
            return AESGCM(kek).decrypt(nonce, sealed, DEK_ASSOCIATED_DATA)
        except InvalidTag as exc:
            raise KeyUnavailableError("wrapped data key failed authentication") from exc

    # --- s3 access-key secrets -----------------------------------------

    def seal_secret(self, data: bytes) -> bytes:
        """Seal an arbitrary-length secret under the current master key.

        Unlike `wrap_kek`, this accepts any length, so it can hold an S3
        access-key secret. The nonce is prepended, as everywhere else.
        """
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._current).encrypt(nonce, data, S3_SECRET_ASSOCIATED_DATA)
        return nonce + sealed

    def unseal_secret(self, sealed: bytes, *, master_key_id: str) -> bytes:
        key = self._key_for(master_key_id)
        nonce, ciphertext = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, S3_SECRET_ASSOCIATED_DATA)
        except InvalidTag as exc:
            raise KeyUnavailableError("sealed secret failed authentication") from exc

    def _key_for(self, key_id: str) -> bytes:
        if key_id == self._current_id:
            return self._current
        if self._previous is not None and key_id == self._previous_id:
            return self._previous
        raise KeyUnavailableError("no master key matches the wrapped key")
