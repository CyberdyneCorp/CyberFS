"""Wrapped key material.

The three-level envelope from `content-encryption/spec.md`:

    MASTER_KEY  ->  wraps a per-user KEK  ->  wraps a per-file DEK

Only wrapped forms are ever persisted. Unwrapped keys exist in process memory
for the duration of a request and nowhere else -- not in Postgres, not in
MinIO, not in Redis, not in a log line.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class UserKey:
    """A user's key-encryption key, sealed under `MASTER_KEY`."""

    user_id: uuid.UUID
    wrapped_kek: bytes
    #: Which master key sealed it, so rotation can find what still needs rewrapping.
    master_key_id: str
    created_at: datetime
    rotated_at: datetime | None = None

    def rewrapped(self, wrapped_kek: bytes, master_key_id: str, now: datetime) -> UserKey:
        return UserKey(
            user_id=self.user_id,
            wrapped_kek=wrapped_kek,
            master_key_id=master_key_id,
            created_at=self.created_at,
            rotated_at=now,
        )


@dataclass(frozen=True, slots=True)
class WrappedDataKey:
    """One file's data key, sealed under one user's KEK.

    There is a row per (node, subject) that may read the file: the owner, plus
    every share recipient. Revoking a grant deletes that subject's row in the
    same transaction, so a revoked recipient has no key to unwrap even if they
    replay a captured request.
    """

    id: uuid.UUID
    node_id: uuid.UUID
    #: CyberdyneAuth subject whose KEK seals this copy.
    subject: str
    wrapped_dek: bytes
    created_at: datetime

    def rewrapped_for(
        self, subject: str, wrapped_dek: bytes, now: datetime, key_id: uuid.UUID
    ) -> WrappedDataKey:
        """A copy of the same DEK sealed for another user.

        Sharing rewraps key material; it never decrypts content to storage.
        """
        return WrappedDataKey(
            id=key_id,
            node_id=self.node_id,
            subject=subject,
            wrapped_dek=wrapped_dek,
            created_at=now,
        )
