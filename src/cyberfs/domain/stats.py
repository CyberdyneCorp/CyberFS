"""Administrative statistics.

Metadata only, by construction: there is no field here that could hold file
content, a preview, or key material. `admin-dashboard/spec.md` requires the
admin surface to be metadata-only, and the shape of these types is the first
line of that guarantee.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class UserStorage:
    """One user's consumption, split the way the dashboard reports it."""

    user_id: uuid.UUID
    subject: str
    quota_bytes: int
    live_bytes: int = 0
    trashed_bytes: int = 0
    version_bytes: int = 0
    file_count: int = 0
    folder_count: int = 0
    encrypted_file_count: int = 0
    encrypted_bytes: int = 0
    grants_given: int = 0
    grants_received: int = 0
    is_admin: bool = False
    created_at: datetime | None = None
    last_seen_at: datetime | None = None

    @property
    def used_bytes(self) -> int:
        return self.live_bytes + self.trashed_bytes + self.version_bytes

    @property
    def percent_used(self) -> float:
        if self.quota_bytes <= 0:
            return 0.0
        return round(100 * self.used_bytes / self.quota_bytes, 2)

    @property
    def over_quota(self) -> bool:
        return self.used_bytes > self.quota_bytes

    @property
    def encrypted_share(self) -> float:
        """What fraction of this user's bytes are sealed."""
        if self.live_bytes <= 0:
            return 0.0
        return round(100 * self.encrypted_bytes / self.live_bytes, 2)


@dataclass(frozen=True, slots=True)
class ContentTypeUsage:
    content_type: str
    file_count: int
    bytes: int


@dataclass(frozen=True, slots=True)
class GrowthPoint:
    day: date
    files_added: int
    bytes_added: int


@dataclass(frozen=True, slots=True)
class TenantStatistics:
    """Deployment-wide totals. Never any per-file detail."""

    total_bytes: int = 0
    live_bytes: int = 0
    trashed_bytes: int = 0
    version_bytes: int = 0
    file_count: int = 0
    folder_count: int = 0
    user_count: int = 0
    active_user_count: int = 0
    encrypted_file_count: int = 0
    encrypted_bytes: int = 0
    public_link_count: int = 0
    grant_count: int = 0
    content_types: tuple[ContentTypeUsage, ...] = field(default=())
    growth: tuple[GrowthPoint, ...] = field(default=())
    top_consumers: tuple[UserStorage, ...] = field(default=())

    @property
    def encrypted_share(self) -> float:
        if self.live_bytes <= 0:
            return 0.0
        return round(100 * self.encrypted_bytes / self.live_bytes, 2)


@dataclass(frozen=True, slots=True)
class JobStatus:
    """The last run of a background job, for the operational view."""

    name: str
    last_run_at: datetime | None = None
    outcome: str | None = None
    duration_seconds: float | None = None
    detail: str | None = None

    @property
    def has_run(self) -> bool:
        return self.last_run_at is not None
