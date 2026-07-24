"""Repository ports.

Protocols owned by the domain and implemented by outbound adapters, so use
cases talk to these and never to SQLAlchemy. The signatures deliberately
express *what* the tree needs -- ancestor walks, subtree reads, sibling-name
lookups -- rather than exposing a query builder, which is what keeps the
adjacency-list-versus-closure-table decision swappable.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from cyberfs.domain.activity import ActivityEntry, ActivityRollup
from cyberfs.domain.audit import AuditRecord
from cyberfs.domain.keys import UserKey, WrappedDataKey
from cyberfs.domain.nodes import FileVersion, Node
from cyberfs.domain.sharing import Grant, PublicLink, Role
from cyberfs.domain.stats import TenantStatistics, UserStorage
from cyberfs.domain.users import QuotaUsage, User


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of a listing, with an opaque cursor for the next."""

    items: tuple[T, ...]
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


class UserRepository(Protocol):
    async def get(self, user_id: uuid.UUID) -> User | None: ...
    async def get_by_subject(self, subject: str) -> User | None: ...
    async def add(self, user: User) -> None: ...
    async def update(self, user: User) -> None: ...
    async def list_all(self, *, limit: int, cursor: str | None = None) -> Page[User]: ...


class NodeRepository(Protocol):
    async def get(self, node_id: uuid.UUID) -> Node | None: ...
    async def add(self, node: Node) -> None: ...
    async def update(self, node: Node) -> None: ...

    async def get_child_by_name(self, parent_id: uuid.UUID, normalized_name: str) -> Node | None:
        """Sibling-name lookup for the uniqueness check. Excludes deleted rows."""
        ...

    async def list_children(
        self,
        parent_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None = None,
        include_deleted: bool = False,
    ) -> Page[Node]: ...

    async def ancestors(self, node_id: uuid.UUID, *, max_depth: int) -> tuple[Node, ...]:
        """Every ancestor, root first, excluding the node itself.

        Bounded by `max_depth`: a corrupted parent link must surface as an
        error rather than spin forever.
        """
        ...

    async def descendants(
        self, node_id: uuid.UUID, *, max_depth: int, include_deleted: bool = False
    ) -> tuple[Node, ...]: ...

    async def is_ancestor_of(self, candidate_id: uuid.UUID, node_id: uuid.UUID) -> bool:
        """Cycle check for a move, evaluated inside the transaction."""
        ...

    async def soft_delete_subtree(self, node_id: uuid.UUID, now: datetime) -> int: ...
    async def list_trashed_before(self, cutoff: datetime, *, limit: int) -> tuple[Node, ...]: ...
    async def delete_permanently(self, node_id: uuid.UUID) -> None: ...

    async def search_by_name(self, subject: str, term: str, *, limit: int) -> tuple[Node, ...]:
        """Metadata-only search, scoped to what `subject` may see."""
        ...


class FileVersionRepository(Protocol):
    async def get(self, version_id: uuid.UUID) -> FileVersion | None: ...
    async def add(self, version: FileVersion) -> None: ...
    async def list_for_node(self, node_id: uuid.UUID) -> tuple[FileVersion, ...]: ...
    async def next_sequence(self, node_id: uuid.UUID) -> int: ...
    async def delete(self, version_id: uuid.UUID) -> None: ...

    async def prune_beyond(self, node_id: uuid.UUID, keep: int) -> tuple[FileVersion, ...]:
        """Drop the oldest versions past the retention count, returning them
        so their objects and quota can be released."""
        ...


class GrantRepository(Protocol):
    async def get(self, grant_id: uuid.UUID) -> Grant | None: ...
    async def add(self, grant: Grant) -> None: ...
    async def update(self, grant: Grant) -> None: ...
    async def delete(self, grant_id: uuid.UUID) -> None: ...
    async def find(self, node_id: uuid.UUID, subject: str) -> Grant | None: ...
    async def list_for_node(self, node_id: uuid.UUID) -> tuple[Grant, ...]: ...

    async def list_for_subject(self, subject: str) -> tuple[Grant, ...]:
        """Every grant held by a subject -- the "shared with me" roots."""
        ...

    async def highest_role_over(self, subject: str, node_ids: Sequence[uuid.UUID]) -> Role | None:
        """The best role `subject` holds anywhere along a path.

        Takes the whole ancestor chain at once so effective permission costs
        one query rather than one per level.
        """
        ...

    async def delete_for_node(self, node_id: uuid.UUID) -> int: ...


class PublicLinkRepository(Protocol):
    async def get(self, link_id: uuid.UUID) -> PublicLink | None: ...
    async def get_by_token_hash(self, token_hash: str) -> PublicLink | None: ...
    async def add(self, link: PublicLink) -> None: ...
    async def update(self, link: PublicLink) -> None: ...
    async def list_for_node(self, node_id: uuid.UUID) -> tuple[PublicLink, ...]: ...
    async def list_active(self, *, limit: int, cursor: str | None = None) -> Page[PublicLink]: ...


class KeyRepository(Protocol):
    async def get_user_key(self, user_id: uuid.UUID) -> UserKey | None: ...
    async def add_user_key(self, key: UserKey) -> None: ...
    async def update_user_key(self, key: UserKey) -> None: ...

    async def list_user_keys_wrapped_under(
        self, master_key_id: str, *, limit: int
    ) -> tuple[UserKey, ...]:
        """Drives resumable `MASTER_KEY` rotation."""
        ...

    async def get_data_key(self, node_id: uuid.UUID, subject: str) -> WrappedDataKey | None: ...
    async def add_data_key(self, key: WrappedDataKey) -> None: ...
    async def list_data_keys(self, node_id: uuid.UUID) -> tuple[WrappedDataKey, ...]: ...
    async def delete_data_key(self, node_id: uuid.UUID, subject: str) -> None: ...
    async def delete_data_keys_for_node(self, node_id: uuid.UUID) -> int: ...


class QuotaRepository(Protocol):
    async def get(self, user_id: uuid.UUID) -> QuotaUsage | None: ...
    async def add(self, usage: QuotaUsage) -> None: ...
    async def update(self, usage: QuotaUsage) -> None: ...

    async def recompute(self, user_id: uuid.UUID) -> QuotaUsage:
        """Derive usage from node and version rows, for the reconciliation job."""
        ...


class AuditRepository(Protocol):
    async def add(self, record: AuditRecord) -> None: ...

    async def query(
        self,
        *,
        actor_subject: str | None = None,
        action: str | None = None,
        target_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int,
        cursor: str | None = None,
    ) -> Page[AuditRecord]:
        """Read-only. There is deliberately no update or delete through the API:
        audit records are immutable, even for administrators."""
        ...

    async def prune_activity(self, cutoff: datetime, *, actions: Sequence[str]) -> int:
        """Delete activity records older than `cutoff`, returning how many went.

        The one deliberate delete on this table, and it is restricted: only the
        activity actions are ever passed, so security records -- denials,
        grants, transfers, encryption changes, admin actions -- are untouched.
        This is retention pruning, not a hole in the append-only contract.
        """
        ...


class ActivityQueries(Protocol):
    """Aggregate and feed reads for a user's own activity.

    Every method is scoped to a single `actor_subject`: there is no shape here
    that could return another user's operations.
    """

    async def summary(
        self, *, actor_subject: str, since: datetime, until: datetime
    ) -> ActivityRollup:
        """Counts per action, plaintext byte totals, and the busiest day.

        Answerable from the `(actor_subject, occurred_at)` index rather than a
        full scan.
        """
        ...

    async def feed(
        self,
        *,
        actor_subject: str,
        since: datetime,
        until: datetime,
        action: str | None = None,
        limit: int,
        cursor: str | None = None,
    ) -> Page[ActivityEntry]:
        """Individual operations, newest first, optionally filtered by action.

        A node the caller does not own -- or one that has since been purged --
        is returned by id with no name.
        """
        ...


class AdminQueries(Protocol):
    """Aggregate reads for the admin surface. Sums and counts only."""

    async def user_storage(self, user_id: uuid.UUID) -> UserStorage | None: ...
    async def all_user_storage(self, *, limit: int) -> tuple[UserStorage, ...]: ...
    async def tenant(self, **kwargs: Any) -> TenantStatistics: ...

    async def reconcile_total(self) -> int:
        """Sum of live file sizes, so reported totals can be checked."""
        ...


class UnitOfWork(Protocol):
    """One transaction, owned by the application layer.

    Repositories are reached through it so a use case's writes commit or roll
    back together -- a grant and its key rewrap, a purge and its quota release.
    """

    users: UserRepository
    nodes: NodeRepository
    versions: FileVersionRepository
    grants: GrantRepository
    public_links: PublicLinkRepository
    keys: KeyRepository
    quotas: QuotaRepository
    audit: AuditRepository
    #: Read-only aggregates for the admin surface. Sums and counts only --
    #: there is no method here that could return content.
    admin: AdminQueries
    #: Read-only aggregates for a caller's own activity. Self-scoped by
    #: `actor_subject`; no method here can return another user's operations.
    activity: ActivityQueries

    async def __aenter__(self) -> UnitOfWork: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...

    async def flush(self) -> None:
        """Push pending writes without committing.

        Required where one row references another: the mapping declares no ORM
        relationships (rows stay dumb, behaviour lives on the domain entities),
        so the ORM has no dependency graph to order inserts by and would
        otherwise emit them in mapper order. A use case that adds a user and
        then something owned by that user must flush between the two.
        """
        ...

    async def lock_subtree(self, node_id: uuid.UUID) -> None:
        """Serialize concurrent moves so two of them cannot jointly create a
        cycle that each check passed individually."""
        ...
