"""S3 access-key credentials.

An access key is a *credential*, not an identity: it resolves to an existing
CyberdyneAuth subject and grants nothing of its own -- see
`authentication/spec.md`, "S3 access keys are a credential, not an identity".

On the secret at rest. `domain/links.py` stores a link *token* as a plain
SHA-256 and verifies by hashing the presented token: no secret is needed
server-side because the check is a one-way comparison. SigV4 is different --
the server must recompute an HMAC chain keyed by the *original* secret, which a
one-way hash cannot yield. So the secret is kept **sealed under `MASTER_KEY`**
(exactly as `UserKey.wrapped_kek` is): recoverable only with the master key,
which lives outside the database, so a database leak alone reveals nothing.
`sealed_secret` is opaque bytes to the domain; sealing and unsealing happen
behind the `KeyProvider` port. The plaintext secret exists only in the mint
response and is never persisted.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, replace
from datetime import datetime

#: `AKIA` mirrors AWS's access-key-id prefix so tooling that pattern-matches on
#: it (and humans) recognise the credential on sight. The suffix is 16 hex
#: characters of CSPRNG output, for a 20-character id like AWS's own.
KEY_ID_PREFIX = "AKIA"
KEY_ID_RANDOM_BYTES = 8

#: 30 bytes of entropy, URL-safe base64 -> a ~40-character secret, comfortably
#: past any brute-force concern. This is the exact string an S3 client signs
#: with, so it must round-trip through sealing unchanged.
SECRET_ENTROPY_BYTES = 30


def generate_key_id() -> str:
    """A fresh, unguessable access-key id."""
    return KEY_ID_PREFIX + secrets.token_hex(KEY_ID_RANDOM_BYTES).upper()


def generate_secret() -> str:
    """A fresh, high-entropy secret access key, shown to the caller once."""
    return secrets.token_urlsafe(SECRET_ENTROPY_BYTES)


@dataclass(frozen=True, slots=True)
class S3AccessKey:
    """One access-key credential belonging to a subject.

    `sealed_secret` is the secret sealed under `MASTER_KEY`; the plaintext is
    never stored on this entity. Revocation is a timestamp, so it takes effect
    the instant it is written -- the next signed request finds `is_active`
    false and is refused, with no cache to expire.
    """

    key_id: str
    sealed_secret: bytes
    #: Which master key sealed the secret, so a rotation can find what to
    #: re-seal and both keys stay usable while a rotation is in flight.
    secret_master_key_id: str
    label: str
    owner_id: uuid.UUID
    #: The CyberdyneAuth subject a request signed with this key resolves to.
    owner_subject: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        """Whether the key may still authenticate a request."""
        return self.revoked_at is None

    def with_last_used(self, now: datetime) -> S3AccessKey:
        """A copy stamped as just used, so idle credentials can be found and
        retired. Written by the verifier on every authenticated request."""
        return replace(self, last_used_at=now)

    def revoked(self, now: datetime) -> S3AccessKey:
        """A copy marked revoked as of `now`. Idempotent: an already-revoked
        key keeps its original revocation time."""
        if self.revoked_at is not None:
            return self
        return replace(self, revoked_at=now)
