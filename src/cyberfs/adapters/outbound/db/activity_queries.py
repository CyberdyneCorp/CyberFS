"""Aggregate and feed queries behind a user's own activity.

Mirrors `admin_queries.py`: the summary sums and counts, and the feed resolves
a node name only when the calling user owns the node. Cross-user and purged
nodes come back as an id with no name, which is what keeps "names appear only
for nodes the user owns" a property of the SQL rather than of the serializer.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, String, cast, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from cyberfs.adapters.outbound.db import models as m
from cyberfs.adapters.outbound.db.repositories import decode_cursor, encode_cursor
from cyberfs.domain.activity import (
    ActivityCount,
    ActivityEntry,
    ActivityRollup,
    build_rollup,
)
from cyberfs.domain.audit import AuditAction, AuditProtocol
from cyberfs.domain.ports.repositories import Page


class SqlActivityQueries:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- summary -------------------------------------------------------

    async def summary(
        self, *, actor_subject: str, since: datetime, until: datetime
    ) -> ActivityRollup:
        day = cast(func.date_trunc("day", m.AuditRow.occurred_at), String).label("day")
        result = await self._session.execute(
            select(
                m.AuditRow.action.label("action"),
                day,
                func.count().label("n"),
                func.coalesce(
                    func.sum(cast(m.AuditRow.context["bytes"].astext, BigInteger)), 0
                ).label("bytes"),
            )
            .where(
                m.AuditRow.actor_subject == actor_subject,
                m.AuditRow.occurred_at >= since,
                m.AuditRow.occurred_at < until,
            )
            .group_by(m.AuditRow.action, literal_column("day"))
        )
        counts = [
            ActivityCount(
                action=AuditAction(row.action),
                day=_as_date(str(row.day)),
                count=int(row.n),
                bytes=int(row.bytes or 0),
            )
            for row in result
        ]
        return build_rollup(counts, window_start=since, window_end=until)

    # --- feed ----------------------------------------------------------

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
        rows = await self._feed_rows(
            actor_subject=actor_subject,
            since=since,
            until=until,
            action=action,
            limit=limit,
            cursor=cursor,
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        names = await self._owned_names(actor_subject, visible)
        entries = tuple(_to_entry(row, names) for row in visible)
        next_cursor = (
            encode_cursor(visible[-1].occurred_at.isoformat()) if has_more and visible else None
        )
        return Page(items=entries, next_cursor=next_cursor)

    async def _feed_rows(
        self,
        *,
        actor_subject: str,
        since: datetime,
        until: datetime,
        action: str | None,
        limit: int,
        cursor: str | None,
    ) -> list[m.AuditRow]:
        stmt = (
            select(m.AuditRow)
            .where(
                m.AuditRow.actor_subject == actor_subject,
                m.AuditRow.occurred_at >= since,
                m.AuditRow.occurred_at < until,
            )
            .order_by(m.AuditRow.occurred_at.desc(), m.AuditRow.id)
        )
        if action is not None:
            stmt = stmt.where(m.AuditRow.action == action)
        if cursor is not None:
            edge = datetime.fromisoformat(decode_cursor(cursor))
            stmt = stmt.where(m.AuditRow.occurred_at < edge)
        result = await self._session.execute(stmt.limit(limit + 1))
        return list(result.scalars())

    async def _owned_names(self, actor_subject: str, rows: list[m.AuditRow]) -> dict[str, str]:
        """Names of the nodes in `rows` that the actor owns, keyed by id string.

        A node the actor does not own -- or one already purged -- is absent, so
        the entry falls back to an id with no name.
        """
        node_ids = _node_ids(rows)
        if not node_ids:
            return {}
        actor_id = await self._session.scalar(
            select(m.UserRow.id).where(m.UserRow.subject == actor_subject)
        )
        if actor_id is None:
            return {}
        result = await self._session.execute(
            select(m.NodeRow.id, m.NodeRow.name).where(
                m.NodeRow.id.in_(node_ids), m.NodeRow.owner_id == actor_id
            )
        )
        return {str(row.id): row.name for row in result}


def _node_ids(rows: list[m.AuditRow]) -> list[uuid.UUID]:
    parsed: list[uuid.UUID] = []
    for row in rows:
        node_id = _as_uuid(row.target_id)
        if node_id is not None:
            parsed.append(node_id)
    return parsed


def _to_entry(row: m.AuditRow, names: dict[str, str]) -> ActivityEntry:
    return ActivityEntry(
        action=AuditAction(row.action),
        occurred_at=row.occurred_at,
        node_id=row.target_id,
        node_name=names.get(row.target_id) if row.target_id is not None else None,
        protocol=AuditProtocol(row.protocol),
    )


def _as_uuid(raw: str | None) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _as_date(raw: str) -> date:
    return datetime.fromisoformat(raw.replace(" ", "T")).date()
