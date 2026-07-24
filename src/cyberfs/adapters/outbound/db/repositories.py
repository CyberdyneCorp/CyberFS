"""SQLAlchemy repositories.

The tree walks use recursive CTEs. `design.md` chose an adjacency list over a
closure table because rename and move stay single-row writes, and because the
one genuinely hot ancestor walk -- effective-permission resolution -- is cached
in Redis. Every recursion is depth-bounded, so a corrupted parent link surfaces
as a bounded result rather than a hung request.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Select,
    case,
    delete,
    func,
    literal,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from cyberfs.adapters.outbound.db import mappers
from cyberfs.adapters.outbound.db import models as m
from cyberfs.domain.audit import AuditRecord
from cyberfs.domain.errors import ValidationError
from cyberfs.domain.keys import UserKey, WrappedDataKey
from cyberfs.domain.nodes import FileVersion, Node, NodeKind
from cyberfs.domain.ports.repositories import Page
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.domain.sharing import Grant, PublicLink, Role
from cyberfs.domain.users import QuotaUsage, User


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> str:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("cursor is not valid") from exc


class SqlUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        row = await self._session.get(m.UserRow, user_id)
        return mappers.user_from_row(row) if row else None

    async def get_by_subject(self, subject: str) -> User | None:
        result = await self._session.execute(select(m.UserRow).where(m.UserRow.subject == subject))
        row = result.scalar_one_or_none()
        return mappers.user_from_row(row) if row else None

    async def add(self, user: User) -> None:
        self._session.add(mappers.user_to_row(user))

    async def update(self, user: User) -> None:
        row = await self._session.get(m.UserRow, user.id)
        if row is not None:
            mappers.apply_user(row, user)

    async def list_all(self, *, limit: int, cursor: str | None = None) -> Page[User]:
        stmt = select(m.UserRow).order_by(m.UserRow.subject).limit(limit + 1)
        if cursor is not None:
            stmt = stmt.where(m.UserRow.subject > decode_cursor(cursor))
        rows = list((await self._session.execute(stmt)).scalars())
        return _paginate(rows, limit, mappers.user_from_row, lambda r: r.subject)


class SqlNodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, node_id: uuid.UUID) -> Node | None:
        row = await self._session.get(m.NodeRow, node_id)
        return mappers.node_from_row(row) if row else None

    async def add(self, node: Node) -> None:
        self._session.add(mappers.node_to_row(node))

    async def update(self, node: Node) -> None:
        row = await self._session.get(m.NodeRow, node.id)
        if row is not None:
            mappers.apply_node(row, node)

    async def get_child_by_name(self, parent_id: uuid.UUID, normalized_name: str) -> Node | None:
        result = await self._session.execute(
            select(m.NodeRow).where(
                m.NodeRow.parent_id == parent_id,
                m.NodeRow.normalized_name == normalized_name,
                m.NodeRow.deleted_at.is_(None),
            )
        )
        row = result.scalar_one_or_none()
        return mappers.node_from_row(row) if row else None

    async def list_children(
        self,
        parent_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None = None,
        include_deleted: bool = False,
    ) -> Page[Node]:
        stmt: Select[tuple[m.NodeRow]] = select(m.NodeRow).where(m.NodeRow.parent_id == parent_id)
        if not include_deleted:
            stmt = stmt.where(m.NodeRow.deleted_at.is_(None))
        # Folders before files, then name. Ordering on the raw `kind` string
        # would put files first ("file" < "folder"), so the rank is explicit.
        # A deterministic order is what makes a cursor meaningful across pages.
        stmt = stmt.order_by(_KIND_RANK, m.NodeRow.normalized_name, m.NodeRow.id)
        if cursor is not None:
            stmt = stmt.where(_child_cursor_predicate(decode_cursor(cursor)))
        rows = list((await self._session.execute(stmt.limit(limit + 1))).scalars())
        return _paginate(
            rows,
            limit,
            mappers.node_from_row,
            lambda r: f"{kind_rank(r.kind)}\x1f{r.normalized_name}\x1f{r.id}",
        )

    async def ancestors(self, node_id: uuid.UUID, *, max_depth: int) -> tuple[Node, ...]:
        """Walk to the root, root first, excluding the node itself."""
        base = (
            select(m.NodeRow, literal(0).label("depth"))
            .where(m.NodeRow.id == node_id)
            .cte("chain", recursive=True)
        )
        parent = m.NodeRow.__table__.alias("parent")
        chain = base.union_all(
            select(parent, (base.c.depth + 1).label("depth")).where(
                parent.c.id == base.c.parent_id,
                base.c.depth < max_depth,
            )
        )
        result = await self._session.execute(
            select(chain).where(chain.c.id != node_id).order_by(chain.c.depth.desc())
        )
        return tuple(mappers.node_from_mapping(dict(r)) for r in result.mappings())

    async def descendants(
        self, node_id: uuid.UUID, *, max_depth: int, include_deleted: bool = False
    ) -> tuple[Node, ...]:
        base = (
            select(m.NodeRow, literal(0).label("depth"))
            .where(m.NodeRow.parent_id == node_id)
            .cte("subtree", recursive=True)
        )
        child = m.NodeRow.__table__.alias("child")
        subtree = base.union_all(
            select(child, (base.c.depth + 1).label("depth")).where(
                child.c.parent_id == base.c.id,
                base.c.depth < max_depth,
            )
        )
        stmt = select(subtree)
        if not include_deleted:
            stmt = stmt.where(subtree.c.deleted_at.is_(None))
        result = await self._session.execute(stmt.order_by(subtree.c.depth))
        return tuple(mappers.node_from_mapping(dict(r)) for r in result.mappings())

    async def is_ancestor_of(self, candidate_id: uuid.UUID, node_id: uuid.UUID) -> bool:
        """Whether `candidate_id` appears above `node_id`.

        The cycle guard for a move, evaluated inside the transaction that
        performs it.
        """
        if candidate_id == node_id:
            return True
        chain = await self.ancestors(node_id, max_depth=_CYCLE_GUARD_DEPTH)
        return any(node.id == candidate_id for node in chain)

    async def soft_delete_subtree(self, node_id: uuid.UUID, now: datetime) -> int:
        subtree = await self.descendants(node_id, max_depth=_CYCLE_GUARD_DEPTH)
        ids = [node_id, *(n.id for n in subtree)]
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(m.NodeRow)
                .where(m.NodeRow.id.in_(ids), m.NodeRow.deleted_at.is_(None))
                .values(deleted_at=now, updated_at=now, revision=m.NodeRow.revision + 1)
            ),
        )
        return int(result.rowcount or 0)

    async def list_trashed_before(self, cutoff: datetime, *, limit: int) -> tuple[Node, ...]:
        result = await self._session.execute(
            select(m.NodeRow)
            .where(m.NodeRow.deleted_at.is_not(None), m.NodeRow.deleted_at < cutoff)
            .order_by(m.NodeRow.deleted_at)
            .limit(limit)
        )
        return tuple(mappers.node_from_row(r) for r in result.scalars())

    async def delete_permanently(self, node_id: uuid.UUID) -> None:
        await self._session.execute(delete(m.NodeRow).where(m.NodeRow.id == node_id))

    async def search_by_name(self, subject: str, term: str, *, limit: int) -> tuple[Node, ...]:
        """Metadata-only. Content is never indexed or matched."""
        owned = select(m.UserRow.id).where(m.UserRow.subject == subject).scalar_subquery()
        granted = select(m.GrantRow.node_id).where(m.GrantRow.subject == subject)
        result = await self._session.execute(
            select(m.NodeRow)
            .where(
                m.NodeRow.deleted_at.is_(None),
                m.NodeRow.normalized_name.ilike(f"%{_escape_like(term)}%"),
                (m.NodeRow.owner_id == owned) | (m.NodeRow.id.in_(granted)),
            )
            .order_by(m.NodeRow.normalized_name)
            .limit(limit)
        )
        return tuple(mappers.node_from_row(r) for r in result.scalars())


class SqlKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_user_key(self, user_id: uuid.UUID) -> UserKey | None:
        row = await self._session.get(m.UserKeyRow, user_id)
        return mappers.user_key_from_row(row) if row else None

    async def add_user_key(self, key: UserKey) -> None:
        self._session.add(mappers.user_key_to_row(key))

    async def update_user_key(self, key: UserKey) -> None:
        row = await self._session.get(m.UserKeyRow, key.user_id)
        if row is not None:
            row.wrapped_kek = key.wrapped_kek
            row.master_key_id = key.master_key_id
            row.rotated_at = key.rotated_at

    async def list_user_keys_wrapped_under(
        self, master_key_id: str, *, limit: int
    ) -> tuple[UserKey, ...]:
        result = await self._session.execute(
            select(m.UserKeyRow)
            .where(m.UserKeyRow.master_key_id == master_key_id)
            .order_by(m.UserKeyRow.user_id)
            .limit(limit)
        )
        return tuple(mappers.user_key_from_row(r) for r in result.scalars())

    async def get_data_key(self, node_id: uuid.UUID, subject: str) -> WrappedDataKey | None:
        result = await self._session.execute(
            select(m.WrappedDataKeyRow).where(
                m.WrappedDataKeyRow.node_id == node_id,
                m.WrappedDataKeyRow.subject == subject,
            )
        )
        row = result.scalar_one_or_none()
        return mappers.data_key_from_row(row) if row else None

    async def add_data_key(self, key: WrappedDataKey) -> None:
        self._session.add(mappers.data_key_to_row(key))

    async def list_data_keys(self, node_id: uuid.UUID) -> tuple[WrappedDataKey, ...]:
        result = await self._session.execute(
            select(m.WrappedDataKeyRow).where(m.WrappedDataKeyRow.node_id == node_id)
        )
        return tuple(mappers.data_key_from_row(r) for r in result.scalars())

    async def delete_data_key(self, node_id: uuid.UUID, subject: str) -> None:
        await self._session.execute(
            delete(m.WrappedDataKeyRow).where(
                m.WrappedDataKeyRow.node_id == node_id,
                m.WrappedDataKeyRow.subject == subject,
            )
        )

    async def delete_data_keys_for_node(self, node_id: uuid.UUID) -> int:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                delete(m.WrappedDataKeyRow).where(m.WrappedDataKeyRow.node_id == node_id)
            ),
        )
        return int(result.rowcount or 0)


class SqlS3AccessKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_key_id(self, key_id: str) -> S3AccessKey | None:
        result = await self._session.execute(
            select(m.S3AccessKeyRow).where(m.S3AccessKeyRow.key_id == key_id)
        )
        row = result.scalar_one_or_none()
        return mappers.s3_access_key_from_row(row) if row else None

    async def add(self, key: S3AccessKey) -> None:
        self._session.add(mappers.s3_access_key_to_row(key))

    async def list_for_subject(self, owner_subject: str) -> tuple[S3AccessKey, ...]:
        result = await self._session.execute(
            select(m.S3AccessKeyRow)
            .where(m.S3AccessKeyRow.owner_subject == owner_subject)
            .order_by(m.S3AccessKeyRow.created_at.desc())
        )
        return tuple(mappers.s3_access_key_from_row(r) for r in result.scalars())

    async def update(self, key: S3AccessKey) -> None:
        result = await self._session.execute(
            select(m.S3AccessKeyRow).where(m.S3AccessKeyRow.key_id == key.key_id)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            mappers.apply_s3_access_key(row, key)


class SqlQuotaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> QuotaUsage | None:
        row = await self._session.get(m.QuotaUsageRow, user_id)
        return mappers.quota_from_row(row) if row else None

    async def add(self, usage: QuotaUsage) -> None:
        self._session.add(mappers.quota_to_row(usage))

    async def update(self, usage: QuotaUsage) -> None:
        row = await self._session.get(m.QuotaUsageRow, usage.user_id)
        if row is not None:
            mappers.apply_quota(row, usage)

    async def recompute(self, user_id: uuid.UUID) -> QuotaUsage:
        """Derive usage from the rows themselves, for reconciliation.

        Live and trashed bytes come from current file sizes; version bytes are
        every retained version other than the current one.
        """
        live = await self._sum_nodes(user_id, deleted=False)
        trashed = await self._sum_nodes(user_id, deleted=True)
        versions = await self._session.scalar(
            select(func.coalesce(func.sum(m.FileVersionRow.size_bytes), 0))
            .select_from(m.FileVersionRow)
            .join(m.NodeRow, m.NodeRow.id == m.FileVersionRow.node_id)
            .where(
                m.FileVersionRow.owner_id == user_id,
                (m.NodeRow.current_version_id.is_(None))
                | (m.FileVersionRow.id != m.NodeRow.current_version_id),
            )
        )
        return QuotaUsage(
            user_id=user_id,
            live_bytes=int(live or 0),
            trashed_bytes=int(trashed or 0),
            version_bytes=int(versions or 0),
        )

    async def _sum_nodes(self, user_id: uuid.UUID, *, deleted: bool) -> int:
        condition = m.NodeRow.deleted_at.is_not(None) if deleted else m.NodeRow.deleted_at.is_(None)
        total = await self._session.scalar(
            select(func.coalesce(func.sum(m.NodeRow.size_bytes), 0)).where(
                m.NodeRow.owner_id == user_id,
                m.NodeRow.kind == "file",
                condition,
            )
        )
        return int(total or 0)


class SqlAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: AuditRecord) -> None:
        self._session.add(mappers.audit_to_row(record))

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
        stmt = select(m.AuditRow).order_by(m.AuditRow.occurred_at.desc(), m.AuditRow.id)
        if actor_subject is not None:
            stmt = stmt.where(m.AuditRow.actor_subject == actor_subject)
        if action is not None:
            stmt = stmt.where(m.AuditRow.action == action)
        if target_id is not None:
            stmt = stmt.where(m.AuditRow.target_id == target_id)
        if since is not None:
            stmt = stmt.where(m.AuditRow.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(m.AuditRow.occurred_at < until)
        if cursor is not None:
            stmt = stmt.where(
                m.AuditRow.occurred_at < datetime.fromisoformat(decode_cursor(cursor))
            )
        rows = list((await self._session.execute(stmt.limit(limit + 1))).scalars())
        return _paginate(rows, limit, mappers.audit_from_row, lambda r: r.occurred_at.isoformat())

    async def prune_activity(self, cutoff: datetime, *, actions: Sequence[str]) -> int:
        """Delete activity records older than `cutoff`, returning the row count.

        Restricted to the actions passed -- always the activity set, never a
        security action -- so evidence records survive under their own longer
        retention.
        """
        if not actions:
            return 0
        result = await self._session.execute(
            delete(m.AuditRow).where(
                m.AuditRow.action.in_(actions), m.AuditRow.occurred_at < cutoff
            )
        )
        return cast(CursorResult[Any], result).rowcount


# --- helpers ---------------------------------------------------------------

#: Recursion ceiling for cycle detection and subtree sweeps. Deliberately far
#: above `MAX_TREE_DEPTH` so a corrupted link is caught by the guard rather
#: than mistaken for a legitimately deep tree.
_CYCLE_GUARD_DEPTH = 512


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def kind_rank(kind: str) -> int:
    """Folders sort before files, whatever their names spell."""
    return 0 if kind == str(NodeKind.FOLDER) else 1


#: The same rank, evaluated in SQL, so ORDER BY and the cursor agree.
_KIND_RANK = case((m.NodeRow.kind == str(NodeKind.FOLDER), 0), else_=1)


def _child_cursor_predicate(raw: str) -> ColumnElement[bool]:
    rank_text, name, node_id = raw.split("\x1f", 2)
    rank = int(rank_text)
    return (
        (_KIND_RANK > rank)
        | ((_KIND_RANK == rank) & (m.NodeRow.normalized_name > name))
        | (
            (_KIND_RANK == rank)
            & (m.NodeRow.normalized_name == name)
            & (m.NodeRow.id > uuid.UUID(node_id))
        )
    )


def _paginate[R, T](
    rows: Sequence[R],
    limit: int,
    to_entity: Callable[[R], T],
    to_cursor: Callable[[R], str],
) -> Page[T]:
    """Trim the sentinel row and derive the next cursor from the last visible one."""
    has_more = len(rows) > limit
    visible = rows[:limit]
    cursor = encode_cursor(to_cursor(visible[-1])) if has_more and visible else None
    return Page(items=tuple(to_entity(r) for r in visible), next_cursor=cursor)


__all__ = [
    "Role",
    "SqlAuditRepository",
    "SqlFileVersionRepository",
    "SqlGrantRepository",
    "SqlKeyRepository",
    "SqlNodeRepository",
    "SqlPublicLinkRepository",
    "SqlQuotaRepository",
    "SqlUserRepository",
    "decode_cursor",
    "encode_cursor",
]


class SqlGrantRepository:
    """Grants, plus the one query effective-permission resolution needs.

    `highest_role_over` takes a whole ancestor chain at once, so resolving a
    caller's role costs a single round trip rather than one per level.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, grant_id: uuid.UUID) -> Grant | None:
        row = await self._session.get(m.GrantRow, grant_id)
        return mappers.grant_from_row(row) if row else None

    async def add(self, grant: Grant) -> None:
        self._session.add(mappers.grant_to_row(grant))

    async def update(self, grant: Grant) -> None:
        row = await self._session.get(m.GrantRow, grant.id)
        if row is not None:
            row.role = int(grant.role)
            row.updated_at = grant.updated_at

    async def delete(self, grant_id: uuid.UUID) -> None:
        await self._session.execute(delete(m.GrantRow).where(m.GrantRow.id == grant_id))

    async def find(self, node_id: uuid.UUID, subject: str) -> Grant | None:
        result = await self._session.execute(
            select(m.GrantRow).where(m.GrantRow.node_id == node_id, m.GrantRow.subject == subject)
        )
        row = result.scalar_one_or_none()
        return mappers.grant_from_row(row) if row else None

    async def list_for_node(self, node_id: uuid.UUID) -> tuple[Grant, ...]:
        result = await self._session.execute(
            select(m.GrantRow).where(m.GrantRow.node_id == node_id).order_by(m.GrantRow.subject)
        )
        return tuple(mappers.grant_from_row(r) for r in result.scalars())

    async def list_for_subject(self, subject: str) -> tuple[Grant, ...]:
        result = await self._session.execute(
            select(m.GrantRow).where(m.GrantRow.subject == subject).order_by(m.GrantRow.created_at)
        )
        return tuple(mappers.grant_from_row(r) for r in result.scalars())

    async def highest_role_over(self, subject: str, node_ids: Sequence[uuid.UUID]) -> Role | None:
        if not node_ids:
            return None
        best = await self._session.scalar(
            select(func.max(m.GrantRow.role)).where(
                m.GrantRow.subject == subject,
                m.GrantRow.node_id.in_(list(node_ids)),
            )
        )
        return Role(best) if best is not None else None

    async def delete_for_node(self, node_id: uuid.UUID) -> int:
        result = cast(
            CursorResult[Any],
            await self._session.execute(delete(m.GrantRow).where(m.GrantRow.node_id == node_id)),
        )
        return int(result.rowcount or 0)


class SqlFileVersionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, version_id: uuid.UUID) -> FileVersion | None:
        row = await self._session.get(m.FileVersionRow, version_id)
        return mappers.version_from_row(row) if row else None

    async def add(self, version: FileVersion) -> None:
        self._session.add(mappers.version_to_row(version))

    async def list_for_node(self, node_id: uuid.UUID) -> tuple[FileVersion, ...]:
        result = await self._session.execute(
            select(m.FileVersionRow)
            .where(m.FileVersionRow.node_id == node_id)
            .order_by(m.FileVersionRow.sequence.desc())
        )
        return tuple(mappers.version_from_row(r) for r in result.scalars())

    async def next_sequence(self, node_id: uuid.UUID) -> int:
        highest = await self._session.scalar(
            select(func.max(m.FileVersionRow.sequence)).where(m.FileVersionRow.node_id == node_id)
        )
        return int(highest or 0) + 1

    async def delete(self, version_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(m.FileVersionRow).where(m.FileVersionRow.id == version_id)
        )

    async def prune_beyond(self, node_id: uuid.UUID, keep: int) -> tuple[FileVersion, ...]:
        """Drop the oldest versions past the retention count.

        Returns them so the caller can delete their objects and release the
        quota they were holding -- pruning metadata alone would orphan bytes.
        """
        versions = await self.list_for_node(node_id)
        doomed = versions[keep:]
        for version in doomed:
            await self.delete(version.id)
        return doomed


class SqlPublicLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, link_id: uuid.UUID) -> PublicLink | None:
        row = await self._session.get(m.PublicLinkRow, link_id)
        return mappers.link_from_row(row) if row else None

    async def get_by_token_hash(self, token_hash: str) -> PublicLink | None:
        result = await self._session.execute(
            select(m.PublicLinkRow).where(m.PublicLinkRow.token_hash == token_hash)
        )
        row = result.scalar_one_or_none()
        return mappers.link_from_row(row) if row else None

    async def add(self, link: PublicLink) -> None:
        self._session.add(mappers.link_to_row(link))

    async def update(self, link: PublicLink) -> None:
        row = await self._session.get(m.PublicLinkRow, link.id)
        if row is not None:
            mappers.apply_link(row, link)

    async def list_for_node(self, node_id: uuid.UUID) -> tuple[PublicLink, ...]:
        result = await self._session.execute(
            select(m.PublicLinkRow)
            .where(m.PublicLinkRow.node_id == node_id)
            .order_by(m.PublicLinkRow.created_at.desc())
        )
        return tuple(mappers.link_from_row(r) for r in result.scalars())

    async def list_active(self, *, limit: int, cursor: str | None = None) -> Page[PublicLink]:
        stmt = (
            select(m.PublicLinkRow)
            .where(m.PublicLinkRow.revoked_at.is_(None))
            .order_by(m.PublicLinkRow.created_at.desc(), m.PublicLinkRow.id)
            .limit(limit + 1)
        )
        rows = list((await self._session.execute(stmt)).scalars())
        return _paginate(rows, limit, mappers.link_from_row, lambda r: r.created_at.isoformat())
