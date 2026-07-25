"""Cryptography ports.

Only the parts provisioning needs land here; framed content sealing arrives
with the encryption capability. Keeping them as ports from the start means the
key hierarchy is never reachable except through an interface the domain owns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from cyberfs.domain.framing import FrameSpan


class KeyProvider(Protocol):
    """Wraps and unwraps key-encryption keys under the deployment master key."""

    @property
    def master_key_id(self) -> str:
        """Identifies the master key currently used for wrapping.

        Recorded on each wrapped KEK so a rotation can find what it still has
        to rewrap, and resume after an interruption.
        """
        ...

    def generate_kek(self) -> bytes:
        """A fresh 256-bit key-encryption key from a cryptographic source."""
        ...

    def wrap_kek(self, kek: bytes) -> bytes: ...

    def unwrap_kek(self, wrapped: bytes, *, master_key_id: str) -> bytes:
        """Unseal a KEK.

        Takes the id of the master key that sealed it so both keys can be
        accepted while a rotation is in flight.
        """
        ...

    def wrap_dek(self, dek: bytes, kek: bytes) -> bytes:
        """Seal a file's data key under a user's key-encryption key."""
        ...

    def unwrap_dek(self, wrapped: bytes, kek: bytes) -> bytes: ...

    def seal_secret(self, data: bytes) -> bytes:
        """Seal an arbitrary-length secret under the current master key.

        Distinct from `wrap_kek`, which is fixed at 32 bytes and so cannot hold
        an S3 access-key secret. Used to store the secret recoverably only with
        the master key -- never in the clear, never in the database alone.
        """
        ...

    def unseal_secret(self, sealed: bytes, *, master_key_id: str) -> bytes:
        """Recover a secret sealed by `seal_secret`, using whichever master key
        sealed it so both are accepted while a rotation is in flight."""
        ...


class ContentCipher(Protocol):
    """Seals and opens file content in authenticated frames."""

    @property
    def frame_bytes(self) -> int: ...

    def generate_key(self) -> bytes:
        """A fresh 256-bit data key, unique to one file version."""
        ...

    def seal(
        self, plaintext: AsyncIterator[bytes], key: bytes, version_id: bytes
    ) -> AsyncIterator[bytes]: ...

    def open(
        self,
        ciphertext: AsyncIterator[bytes],
        key: bytes,
        version_id: bytes,
        *,
        span: FrameSpan | None = None,
    ) -> AsyncIterator[bytes]:
        """Decrypt, verifying every frame. `span` decrypts a sub-range."""
        ...

    def ciphertext_length(self, plaintext_bytes: int) -> int: ...
