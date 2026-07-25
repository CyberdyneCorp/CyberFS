"""Aggregate queries behind the admin surface.

Every query here sums, counts, or groups. None of them selects a file name or
touches an object, which is what makes "administrators see statistics, never
content" a property of the SQL rather than of the serializer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import String, cast, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from cyberfs.adapters.outbound.db import models as m
from cyberfs.domain.stats import (
    ContentTypeUsage,
    GrowthPoint,
    TenantStatistics,
    UserStorage,
)

FILE = "file"
FOLDER = "folder"
UNKNOWN_TYPE = "application/octet-stream"


class SqlAdminQueries:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- per user ------------------------------------------------------

    async def user_storage(self, user_id: uuid.UUID) -> UserStorage | None:
        row = await self._session.get(m.UserRow, user_id)
        if row is None:
            return None
        return await self._build(row)

    async def all_user_storage(self, *, limit: int) -> tuple[UserStorage, ...]:
        result = await self._session.execute(
            select(m.UserRow).order_by(m.UserRow.subject).limit(limit)
        )
        return tuple([await self._build(row) for row in result.scalars()])

    async def _build(self, row: m.UserRow) -> UserStorage:
        counts = await self._node_counts(row.id)
        versions = await self._version_bytes(row.id)
        return UserStorage(
            user_id=row.id,
            subject=row.subject,
            quota_bytes=row.quota_bytes,
            live_bytes=counts["live_bytes"],
            trashed_bytes=counts["trashed_bytes"],
            version_bytes=versions,
            file_count=counts["files"],
            folder_count=counts["folders"],
            encrypted_file_count=counts["encrypted_files"],
            encrypted_bytes=counts["encrypted_bytes"],
            grants_given=await self._grants_given(row.id),
            grants_received=await self._grants_received(row.subject),
            is_admin=row.is_admin,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
        )

    async def _node_counts(self, user_id: uuid.UUID) -> dict[str, int]:
        live = m.NodeRow.deleted_at.is_(None)
        result = await self._session.execute(
            select(
                func.coalesce(
                    func.sum(func.coalesce(m.NodeRow.size_bytes, 0)).filter(
                        live, m.NodeRow.kind == FILE
                    ),
                    0,
                ).label("live_bytes"),
                func.coalesce(
                    func.sum(func.coalesce(m.NodeRow.size_bytes, 0)).filter(
                        m.NodeRow.deleted_at.is_not(None), m.NodeRow.kind == FILE
                    ),
                    0,
                ).label("trashed_bytes"),
                func.count().filter(live, m.NodeRow.kind == FILE).label("files"),
                func.count().filter(live, m.NodeRow.kind == FOLDER).label("folders"),
                func.count()
                .filter(live, m.NodeRow.kind == FILE, m.NodeRow.encrypted.is_(True))
                .label("encrypted_files"),
                func.coalesce(
                    func.sum(func.coalesce(m.NodeRow.size_bytes, 0)).filter(
                        live, m.NodeRow.kind == FILE, m.NodeRow.encrypted.is_(True)
                    ),
                    0,
                ).label("encrypted_bytes"),
            ).where(m.NodeRow.owner_id == user_id)
        )
        row = result.mappings().one()
        return {key: int(value or 0) for key, value in row.items()}

    async def _version_bytes(self, user_id: uuid.UUID) -> int:
        """Retained history: every version that is not the current one."""
        total = await self._session.scalar(
            select(func.coalesce(func.sum(m.FileVersionRow.size_bytes), 0))
            .select_from(m.FileVersionRow)
            .join(m.NodeRow, m.NodeRow.id == m.FileVersionRow.node_id)
            .where(
                m.FileVersionRow.owner_id == user_id,
                (m.NodeRow.current_version_id.is_(None))
                | (m.FileVersionRow.id != m.NodeRow.current_version_id),
            )
        )
        return int(total or 0)

    async def _grants_given(self, user_id: uuid.UUID) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(m.GrantRow)
            .join(m.NodeRow, m.NodeRow.id == m.GrantRow.node_id)
            .where(m.NodeRow.owner_id == user_id)
        )
        return int(total or 0)

    async def _grants_received(self, subject: str) -> int:
        total = await self._session.scalar(
            select(func.count()).select_from(m.GrantRow).where(m.GrantRow.subject == subject)
        )
        return int(total or 0)

    # --- tenant-wide ---------------------------------------------------

    async def tenant(
        self,
        *,
        growth_days: int = 30,
        top_n: int = 10,
        active_within: timedelta = timedelta(days=30),
        now: datetime | None = None,
    ) -> TenantStatistics:
        moment = now or datetime.now(tz=UTC)
        totals = await self._tenant_totals()
        consumers = await self.all_user_storage(limit=1000)
        ranked = sorted(consumers, key=lambda u: u.used_bytes, reverse=True)[:top_n]

        return TenantStatistics(
            total_bytes=totals["live_bytes"] + totals["trashed_bytes"],
            live_bytes=totals["live_bytes"],
            trashed_bytes=totals["trashed_bytes"],
            version_bytes=sum(u.version_bytes for u in consumers),
            file_count=totals["files"],
            folder_count=totals["folders"],
            user_count=len(consumers),
            active_user_count=sum(
                1
                for u in consumers
                if u.last_seen_at is not None and moment - u.last_seen_at <= active_within
            ),
            encrypted_file_count=totals["encrypted_files"],
            encrypted_bytes=totals["encrypted_bytes"],
            public_link_count=await self._active_link_count(moment),
            grant_count=await self._grant_count(),
            content_types=await self.content_types(),
            growth=await self.growth(days=growth_days, now=moment),
            top_consumers=tuple(ranked),
        )

    async def _tenant_totals(self) -> dict[str, int]:
        live = m.NodeRow.deleted_at.is_(None)
        result = await self._session.execute(
            select(
                func.coalesce(
                    func.sum(func.coalesce(m.NodeRow.size_bytes, 0)).filter(
                        live, m.NodeRow.kind == FILE
                    ),
                    0,
                ).label("live_bytes"),
                func.coalesce(
                    func.sum(func.coalesce(m.NodeRow.size_bytes, 0)).filter(
                        m.NodeRow.deleted_at.is_not(None), m.NodeRow.kind == FILE
                    ),
                    0,
                ).label("trashed_bytes"),
                func.count().filter(live, m.NodeRow.kind == FILE).label("files"),
                func.count().filter(live, m.NodeRow.kind == FOLDER).label("folders"),
                func.count()
                .filter(live, m.NodeRow.kind == FILE, m.NodeRow.encrypted.is_(True))
                .label("encrypted_files"),
                func.coalesce(
                    func.sum(func.coalesce(m.NodeRow.size_bytes, 0)).filter(
                        live, m.NodeRow.kind == FILE, m.NodeRow.encrypted.is_(True)
                    ),
                    0,
                ).label("encrypted_bytes"),
            )
        )
        return {key: int(value or 0) for key, value in result.mappings().one().items()}

    async def content_types(self, *, limit: int = 20) -> tuple[ContentTypeUsage, ...]:
        """Distribution by declared type -- a header, never inspected content."""
        # Group and order by the output labels: repeating the expression makes
        # SQLAlchemy emit a second bind parameter, which Postgres then treats
        # as a different expression and rejects.
        result = await self._session.execute(
            select(
                func.coalesce(m.NodeRow.content_type, UNKNOWN_TYPE).label("content_type"),
                func.count().label("files"),
                func.coalesce(func.sum(m.NodeRow.size_bytes), 0).label("bytes"),
            )
            .where(m.NodeRow.kind == FILE, m.NodeRow.deleted_at.is_(None))
            .group_by(literal_column("content_type"))
            .order_by(literal_column("bytes").desc())
            .limit(limit)
        )
        return tuple(
            ContentTypeUsage(
                content_type=str(row.content_type),
                file_count=int(row.files),
                bytes=int(row.bytes),
            )
            for row in result
        )

    async def growth(
        self, *, days: int = 30, now: datetime | None = None
    ) -> tuple[GrowthPoint, ...]:
        """Files and bytes added per day over the window."""
        moment = now or datetime.now(tz=UTC)
        since = moment - timedelta(days=days)
        bucket = cast(func.date_trunc("day", m.NodeRow.created_at), String).label("day")
        result = await self._session.execute(
            select(
                bucket,
                func.count().label("files"),
                func.coalesce(func.sum(m.NodeRow.size_bytes), 0).label("bytes"),
            )
            .where(m.NodeRow.kind == FILE, m.NodeRow.created_at >= since)
            .group_by(literal_column("day"))
            .order_by(literal_column("day"))
        )
        return tuple(
            GrowthPoint(
                day=_as_date(str(row.day)),
                files_added=int(row.files),
                bytes_added=int(row.bytes),
            )
            for row in result
        )

    async def _active_link_count(self, now: datetime) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(m.PublicLinkRow)
            .where(
                m.PublicLinkRow.revoked_at.is_(None),
                (m.PublicLinkRow.expires_at.is_(None)) | (m.PublicLinkRow.expires_at > now),
            )
        )
        return int(total or 0)

    async def _grant_count(self) -> int:
        total = await self._session.scalar(select(func.count()).select_from(m.GrantRow))
        return int(total or 0)

    async def reconcile_total(self) -> int:
        """Sum of live file sizes, for asserting reported figures add up."""
        total = await self._session.scalar(
            select(func.coalesce(func.sum(m.NodeRow.size_bytes), 0)).where(
                m.NodeRow.kind == FILE, m.NodeRow.deleted_at.is_(None)
            )
        )
        return int(total or 0)


def _as_date(raw: str) -> date:
    return datetime.fromisoformat(raw.replace(" ", "T")).date()
