"""Cryptography ports.

Only the parts provisioning needs land here; framed content sealing arrives
with the encryption capability. Keeping them as ports from the start means the
key hierarchy is never reachable except through an interface the domain owns.
"""

from __future__ import annotations

from typing import Protocol


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
