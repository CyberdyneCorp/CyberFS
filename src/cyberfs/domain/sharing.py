"""Grants and public links.

Three totally ordered roles, no deny rule. Effective permission is the maximum
over ownership, direct grants, and ancestor grants -- which keeps "why can Bob
read this?" answerable by walking to the root and taking the highest grant
found. See `design.md`, "Ordered roles with tree inheritance".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum


class Role(IntEnum):
    """Ordered by privilege, so `max()` is the whole resolution rule."""

    VIEWER = 10
    EDITOR = 20
    OWNER = 30

    @property
    def slug(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str) -> Role:
        try:
            return cls[value.strip().upper()]
        except KeyError as exc:
            raise ValueError(f"unknown role: {value!r}") from exc

    # --- capability queries -------------------------------------------

    @property
    def can_read(self) -> bool:
        return self >= Role.VIEWER

    @property
    def can_write(self) -> bool:
        return self >= Role.EDITOR

    @property
    def can_delete(self) -> bool:
        return self >= Role.OWNER

    @property
    def can_share(self) -> bool:
        """Only an owner may re-share; an editor cannot widen access."""
        return self >= Role.OWNER

    @property
    def can_change_encryption(self) -> bool:
        return self >= Role.OWNER


@dataclass(frozen=True, slots=True)
class Grant:
    """A role held by one subject on one node, inherited by its descendants."""

    id: uuid.UUID
    node_id: uuid.UUID
    #: CyberdyneAuth subject of the recipient.
    subject: str
    role: Role
    granted_by: str
    created_at: datetime
    updated_at: datetime

    def with_role(self, role: Role, now: datetime) -> Grant:
        """Regrant replaces the role rather than adding a second grant."""
        return Grant(
            id=self.id,
            node_id=self.node_id,
            subject=self.subject,
            role=role,
            granted_by=self.granted_by,
            created_at=self.created_at,
            updated_at=now,
        )


@dataclass(slots=True)
class PublicLink:
    """Unauthenticated read access to one node and its subtree.

    The token is stored only as a hash: a database leak must not yield working
    links.
    """

    id: uuid.UUID
    node_id: uuid.UUID
    token_hash: str
    created_by: str
    created_at: datetime
    expires_at: datetime | None = None
    passphrase_hash: str | None = None
    revoked_at: datetime | None = None
    access_count: int = 0
    last_accessed_at: datetime | None = None

    @property
    def role(self) -> Role:
        """A public link is always read-only; there is no way to widen it."""
        return Role.VIEWER

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def requires_passphrase(self) -> bool:
        return self.passphrase_hash is not None

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def is_usable(self, now: datetime) -> bool:
        return not self.is_revoked and not self.is_expired(now)

    def revoke(self, now: datetime) -> None:
        if not self.is_revoked:
            self.revoked_at = now

    def record_access(self, now: datetime) -> None:
        self.access_count += 1
        self.last_accessed_at = now
