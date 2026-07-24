"""Local user records and quota accounting.

CyberFS stores no credentials. A user record exists to own a tree, hold a
quota, and anchor a key -- identity itself stays entirely with CyberdyneAuth.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from cyberfs.domain.auth.principal import Org
from cyberfs.domain.errors import QuotaExceededError


@dataclass(slots=True)
class User:
    """A CyberdyneAuth subject that has touched CyberFS at least once."""

    id: uuid.UUID
    #: The CyberdyneAuth `sub`. Stable across email and login-method changes.
    subject: str
    root_folder_id: uuid.UUID
    quota_bytes: int
    created_at: datetime
    updated_at: datetime
    is_admin: bool = False
    org: Org | None = None
    orgs: tuple[Org, ...] = field(default=())
    last_seen_at: datetime | None = None

    def refresh_from_claims(
        self,
        *,
        is_admin: bool,
        org: Org | None,
        orgs: tuple[Org, ...],
        now: datetime,
    ) -> bool:
        """Sync the mutable claims onto the record. Returns whether anything changed."""
        changed = (self.is_admin, self.org, self.orgs) != (is_admin, org, orgs)
        self.is_admin = is_admin
        self.org = org
        self.orgs = orgs
        self.last_seen_at = now
        if changed:
            self.updated_at = now
        return changed


@dataclass(slots=True)
class QuotaUsage:
    """What a user's storage costs them.

    Charged to the node *owner*, and covering live bytes, trashed bytes, and
    retained versions -- deleting a file does not free space until it is
    purged. Share recipients are never charged.
    """

    user_id: uuid.UUID
    live_bytes: int = 0
    trashed_bytes: int = 0
    version_bytes: int = 0
    updated_at: datetime | None = None

    @property
    def total_bytes(self) -> int:
        return self.live_bytes + self.trashed_bytes + self.version_bytes

    def remaining(self, quota_bytes: int) -> int:
        return max(quota_bytes - self.total_bytes, 0)

    def would_exceed(self, quota_bytes: int, additional_bytes: int) -> bool:
        return self.total_bytes + additional_bytes > quota_bytes

    def ensure_room_for(self, quota_bytes: int, additional_bytes: int) -> None:
        if self.would_exceed(quota_bytes, additional_bytes):
            raise QuotaExceededError(
                "upload would exceed the owner's storage quota",
                quota_bytes=quota_bytes,
                used_bytes=self.total_bytes,
                requested_bytes=additional_bytes,
            )

    # --- transitions ---------------------------------------------------
    # Each keeps every bucket at or above zero: a double-release from a retry
    # must not manufacture free space.

    def charge_live(self, delta: int, now: datetime) -> None:
        self.live_bytes = max(self.live_bytes + delta, 0)
        self.updated_at = now

    def charge_versions(self, delta: int, now: datetime) -> None:
        self.version_bytes = max(self.version_bytes + delta, 0)
        self.updated_at = now

    def move_to_trash(self, size_bytes: int, now: datetime) -> None:
        """Soft delete: the bytes are still stored, so they still count."""
        moved = min(size_bytes, self.live_bytes)
        self.live_bytes -= moved
        self.trashed_bytes += moved
        self.updated_at = now

    def restore_from_trash(self, size_bytes: int, now: datetime) -> None:
        moved = min(size_bytes, self.trashed_bytes)
        self.trashed_bytes -= moved
        self.live_bytes += moved
        self.updated_at = now

    def purge_from_trash(self, size_bytes: int, now: datetime) -> None:
        """Hard delete: the only transition that actually frees space."""
        self.trashed_bytes = max(self.trashed_bytes - size_bytes, 0)
        self.updated_at = now

    def reconcile(
        self, *, live_bytes: int, trashed_bytes: int, version_bytes: int, now: datetime
    ) -> bool:
        """Overwrite from a recomputation. Returns whether drift was corrected."""
        drifted = (self.live_bytes, self.trashed_bytes, self.version_bytes) != (
            live_bytes,
            trashed_bytes,
            version_bytes,
        )
        self.live_bytes = live_bytes
        self.trashed_bytes = trashed_bytes
        self.version_bytes = version_bytes
        self.updated_at = now
        return drifted
