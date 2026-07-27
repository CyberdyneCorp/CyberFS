"""Repository ports.

Protocols owned by the domain and implemented by outbound adapters, so use
cases talk to these and never to SQLAlchemy. The signatures deliberately
express *what* the tree needs -- ancestor walks, subtree reads, sibling-name
lookups -- rather than exposing a query builder, which is what keeps the
adjacency-list-versus-closure-table decision swappable.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from cyberfs.domain.activity import ActivityEntry, ActivityRollup
from cyberfs.domain.audit import AuditRecord
from cyberfs.domain.keys import UserKey, WrappedDataKey
from cyberfs.domain.nodes import FileVersion, Node, SubtreeTotals
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.domain.s3.multipart import MultipartPart, MultipartUpload
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

    async def soft_delete_subtree(self, node_id: uuid.UUID, now: datetime) -> tuple[Node, ...]:
        """Trash the node and every live descendant, returning the rows it moved.

        A descendant already in the trash keeps its original `deleted_at` and is
        NOT returned: it was trashed on its own occasion and its bytes are
        already counted there. Returning only what moved is what lets the caller
        charge the quota exactly once -- summing the whole subtree instead
        charges an already-trashed descendant a second time.
        """
        ...

    async def restore_subtree(self, node_id: uuid.UUID, now: datetime) -> tuple[Node, ...]:
        """Lift the node and the descendants trashed together with it, as they
        now stand.

        A delete stamps one `deleted_at` across the whole subtree, so equality
        with the node's own is what identifies its batch. A descendant trashed
        separately beforehand carries a different stamp and stays trashed:
        restoring it would resurrect something deleted on purpose. The rows
        actually cleared come back so the caller can release exactly their
        bytes -- no more.

        The timestamp IS the batch identity, which holds only because every
        delete passes a fresh `utcnow()`. A caller that threaded one `now`
        through two sibling deletes would merge them into one batch and this
        would resurrect the wrong rows -- so do not. Recording an explicit batch
        id is the fix if a bulk delete ever needs to.
        """
        ...

    async def list_trash_entries(
        self,
        owner_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None = None,
        oldest_first: bool = False,
    ) -> Page[Node]:
        """One entry per deletion in that owner's trash, most recent first.

        An entry is a trashed node whose parent is absent or not itself trashed,
        so deleting a folder of four hundred files yields one entry rather than
        four hundred. A descendant could not be presented usefully anyway: its
        ancestors are trashed too, so it has no path in the live tree, and
        restoring it individually reparents it into the owner's root instead of
        reconstructing anything.

        Owner-scoped by signature -- a share confers nothing on a trashed node,
        so there is no shape here that could return another user's trash. Pass
        `oldest_first` for the destruction order emptying the trash uses;
        pagination is deterministic either way.
        """
        ...

    async def count_trash_entries(self, owner_id: uuid.UUID) -> int:
        """How many entries that owner's trash currently holds.

        Exact and unbounded, because it is what a caller's stated count is
        checked against before anything is destroyed.
        """
        ...

    async def delete_batch_totals(
        self, node_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, SubtreeTotals]:
        """Content bytes and node counts of each id's *delete batch*, in one query.

        Named for the batch and not the subtree because that is what it measures:
        only the rows stamped with the id's own `deleted_at`, which is exactly
        what `restore_subtree` would lift. A descendant trashed on its own
        occasion carries a different stamp, so it is excluded here and counted
        once, under its own entry.

        It follows that an id which is **not trashed** has no batch and is simply
        **absent from the result** -- there is no row whose `deleted_at` equals
        NULL. Callers must treat a missing key as "not a trash entry" rather than
        as a subtree of zero bytes.

        One aggregate rather than a recursive walk per entry: a page is bounded
        by `PAGE_SIZE_MAX`, and a thousand walks to render a listing is not.
        """
        ...

    async def ancestor_chains(
        self, node_ids: Sequence[uuid.UUID], *, max_depth: int
    ) -> dict[uuid.UUID, tuple[Node, ...]]:
        """`ancestors` for many nodes at once: one walk, keyed by the id asked for.

        Each chain is root first and excludes the node itself, exactly as
        `ancestors` returns it. It exists because rendering a page of trash
        entries needs a path per entry, and a walk per entry would be up to
        `PAGE_SIZE_MAX` recursive queries for one read.
        """
        ...

    async def list_trashed_before(self, cutoff: datetime, *, limit: int) -> tuple[Node, ...]: ...
    async def delete_permanently(self, node_id: uuid.UUID) -> None: ...

    async def search(
        self,
        subject: str,
        *,
        term: str | None = None,
        tags: Sequence[str] = (),
        key: str | None = None,
        value: str | None = None,
        limit: int,
    ) -> tuple[Node, ...]:
        """Nodes the caller may see, narrowed by every supplied filter."""
        ...

    async def tags_for(self, node_id: uuid.UUID) -> frozenset[str]: ...

    async def replace_tags(self, node_id: uuid.UUID, tags: frozenset[str]) -> None: ...

    async def metadata_for(self, node_id: uuid.UUID) -> dict[str, str]: ...

    async def replace_metadata(self, node_id: uuid.UUID, pairs: Mapping[str, str]) -> None:
        """Make these exactly the pairs a caller can write.

        Reserved keys are outside that: they are metadata CyberFS wrote about the
        node, and a namespace a caller can empty is not one CyberFS can trust. So
        they survive a replace, including a replace of nothing.
        """
        ...

    async def add_tags(self, node_id: uuid.UUID, tags: frozenset[str]) -> None:
        """Add these tags, ignoring the ones already there.

        A row at a time rather than a whole-collection write, so two callers
        adding different tags do not overwrite each other -- the point of a
        partial update.
        """
        ...

    async def remove_tags(self, node_id: uuid.UUID, tags: frozenset[str]) -> None:
        """Drop these tags by name; ones the node does not carry are ignored."""
        ...

    async def set_metadata(self, node_id: uuid.UUID, pairs: Mapping[str, str]) -> None:
        """Write these keys, leaving every key not named byte-identical."""
        ...

    async def remove_metadata_keys(self, node_id: uuid.UUID, keys: frozenset[str]) -> None:
        """Drop these keys; ones the node does not have are ignored.

        No method advances the revision: a partial update bumps it with
        `Node.touch` and persists through `update`, which is correct because it
        read the node under `UnitOfWork.lock_subtree`. A SQL increment would leave
        the in-memory `Node` -- and so the ETag on the response -- one behind.
        """
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

    async def find(self, node_id: uuid.UUID, subject: str) -> Grant | None:
        """One grant by node and subject, pending or not -- the write path's
        read, used by grant/revoke and the rewrap worker."""
        ...

    async def list_for_node(self, node_id: uuid.UUID) -> tuple[Grant, ...]:
        """Every grant on a node, including pending ones: the owner-facing
        listing shows a pending share with its status."""
        ...

    async def list_for_subject(self, subject: str) -> tuple[Grant, ...]:
        """The active grants a subject holds -- the "shared with me" roots.

        Excludes pending grants: a share whose background rewrap has not
        completed confers no access and does not appear in the recipient's view.
        """
        ...

    async def highest_role_over(self, subject: str, node_ids: Sequence[uuid.UUID]) -> Role | None:
        """The best *active* role `subject` holds anywhere along a path.

        Takes the whole ancestor chain at once so effective permission costs
        one query rather than one per level. Pending grants are excluded, so a
        grant whose rewrap is unfinished grants nothing anywhere.
        """
        ...

    async def list_pending(self, *, limit: int) -> tuple[Grant, ...]:
        """The oldest pending grants, bounded -- the rewrap worker's driver."""
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


class S3AccessKeyRepository(Protocol):
    """Access-key credentials, looked up by id on the signing hot path.

    There is deliberately no query returning the sealed secret in bulk: a
    listing is by owner, and only the single `get_by_key_id` on the verifier's
    path ever reads the sealed material.
    """

    async def get_by_key_id(self, key_id: str) -> S3AccessKey | None: ...
    async def add(self, key: S3AccessKey) -> None: ...

    async def list_for_subject(self, owner_subject: str) -> tuple[S3AccessKey, ...]:
        """Every key a subject owns -- for the management surface and rotation.
        Newest first, so the freshest credential heads the list."""
        ...

    async def update(self, key: S3AccessKey) -> None:
        """Persist a revocation or a last-used stamp. Both are timestamp writes
        on an existing row."""
        ...


class MultipartUploadRepository(Protocol):
    """In-flight multipart uploads and their staged parts.

    An upload is looked up by its opaque id; parts are read in bulk for one
    upload. The abandonment scan is answerable from the ``created_at`` index
    rather than a full table scan, so the reaper reclaims uploads left neither
    completed nor aborted.
    """

    async def create(self, upload: MultipartUpload) -> None: ...
    async def get(self, upload_id: str) -> MultipartUpload | None: ...
    async def delete(self, upload_id: str) -> None:
        """Remove the upload and its part rows. Its staged objects are deleted
        separately by the caller, which holds the object store."""
        ...

    async def add_part(self, part: MultipartPart) -> None:
        """Record a staged part, replacing any earlier part with the same
        number -- S3 lets a client re-upload a part before completing."""
        ...

    async def list_parts(self, upload_id: str) -> tuple[MultipartPart, ...]: ...

    async def list_abandoned_before(
        self, cutoff: datetime, *, limit: int
    ) -> tuple[MultipartUpload, ...]:
        """Uploads created before `cutoff` -- the reaper's abandonment sweep."""
        ...


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
    s3_keys: S3AccessKeyRepository
    multipart: MultipartUploadRepository
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
