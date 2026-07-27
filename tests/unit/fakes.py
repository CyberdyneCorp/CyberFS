"""In-memory implementations of the repository ports.

Let use cases be tested as pure logic, with no Postgres. The SQLAlchemy
implementations of the same ports are covered by the integration suite, which
is where real constraint and transaction behaviour actually lives.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from types import TracebackType

from cyberfs.domain.activity import (
    ActivityCount,
    ActivityEntry,
    ActivityRollup,
    build_rollup,
)
from cyberfs.domain.audit import AuditProtocol, AuditRecord
from cyberfs.domain.errors import NotFoundError
from cyberfs.domain.keys import UserKey, WrappedDataKey
from cyberfs.domain.nodes import (
    RESERVED_METADATA_PREFIX,
    FileVersion,
    Node,
    SubtreeTotals,
)
from cyberfs.domain.pagination import (
    decode_cursor,
    decode_keyed_cursor,
    encode_cursor,
    encode_keyed_cursor,
    fingerprint_of,
)
from cyberfs.domain.ports.repositories import Page
from cyberfs.domain.ports.storage import StoredObject
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.domain.s3.multipart import MultipartPart, MultipartUpload
from cyberfs.domain.sharing import Grant, PublicLink, Role
from cyberfs.domain.users import QuotaUsage, User


class FakeUserRepository:
    def __init__(self) -> None:
        self.by_id: dict[uuid.UUID, User] = {}

    async def get(self, user_id: uuid.UUID) -> User | None:
        return self.by_id.get(user_id)

    async def get_by_subject(self, subject: str) -> User | None:
        return next((u for u in self.by_id.values() if u.subject == subject), None)

    async def add(self, user: User) -> None:
        self.by_id[user.id] = user

    async def update(self, user: User) -> None:
        self.by_id[user.id] = user

    async def list_all(self, *, limit: int, cursor: str | None = None) -> Page[User]:
        ordered = sorted(self.by_id.values(), key=lambda u: u.subject)
        return Page(items=tuple(ordered[:limit]))


def _trash_key(node: Node) -> tuple[datetime, uuid.UUID]:
    """The listing's sort key: deletion time, then id as the tie-break."""
    assert node.deleted_at is not None
    return node.deleted_at, node.id


def _trash_fingerprint(owner_id: uuid.UUID, oldest_first: bool) -> str:
    """What the SQL repository binds a trash cursor to, reproduced exactly.

    A laxer fake here would let a cursor that the real listing refuses sail
    through a unit test.
    """
    return fingerprint_of("trash", str(owner_id), "oldest" if oldest_first else "newest")


def _trash_cursor(node: Node, fingerprint: str) -> str:
    stamp, node_id = _trash_key(node)
    return encode_cursor(encode_keyed_cursor(fingerprint, stamp.isoformat(), str(node_id)))


def _trash_edge(cursor: str, fingerprint: str) -> tuple[datetime, uuid.UUID]:
    stamp, node_id = decode_keyed_cursor(decode_cursor(cursor), fingerprint=fingerprint, fields=2)
    return datetime.fromisoformat(stamp), uuid.UUID(node_id)


class FakeNodeRepository:
    def __init__(self) -> None:
        self.by_id: dict[uuid.UUID, Node] = {}
        # The real schema cascades these away with the node row; the fake has no
        # foreign keys, so `delete_permanently` clears them explicitly.
        self.tags: dict[uuid.UUID, frozenset[str]] = {}
        self.metadata: dict[uuid.UUID, dict[str, str]] = {}

    async def get(self, node_id: uuid.UUID) -> Node | None:
        return self.by_id.get(node_id)

    async def add(self, node: Node) -> None:
        self.by_id[node.id] = node

    async def update(self, node: Node) -> None:
        self.by_id[node.id] = node

    async def get_child_by_name(self, parent_id: uuid.UUID, normalized_name: str) -> Node | None:
        return next(
            (
                n
                for n in self.by_id.values()
                if n.parent_id == parent_id
                and n.normalized_name == normalized_name
                and not n.is_deleted
            ),
            None,
        )

    async def list_children(
        self,
        parent_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None = None,
        include_deleted: bool = False,
    ) -> Page[Node]:
        children = [
            n
            for n in self.by_id.values()
            if n.parent_id == parent_id and (include_deleted or not n.is_deleted)
        ]
        # Folders first, matching the SQL repository's explicit rank.
        children.sort(key=lambda n: (0 if n.is_folder else 1, n.normalized_name, str(n.id)))
        return Page(items=tuple(children[:limit]))

    async def ancestors(self, node_id: uuid.UUID, *, max_depth: int) -> tuple[Node, ...]:
        chain: list[Node] = []
        current = self.by_id.get(node_id)
        seen: set[uuid.UUID] = set()
        while current is not None and current.parent_id is not None:
            if current.parent_id in seen or len(chain) >= max_depth:
                break
            seen.add(current.parent_id)
            parent = self.by_id.get(current.parent_id)
            if parent is None:
                break
            chain.append(parent)
            current = parent
        return tuple(reversed(chain))

    async def descendants(
        self, node_id: uuid.UUID, *, max_depth: int, include_deleted: bool = False
    ) -> tuple[Node, ...]:
        found: list[Node] = []
        frontier = [node_id]
        for _ in range(max_depth):
            children = [n for n in self.by_id.values() if n.parent_id in frontier]
            if not children:
                break
            found.extend(n for n in children if include_deleted or not n.is_deleted)
            frontier = [n.id for n in children]
        return tuple(found)

    async def is_ancestor_of(self, candidate_id: uuid.UUID, node_id: uuid.UUID) -> bool:
        if candidate_id == node_id:
            return True
        chain = await self.ancestors(node_id, max_depth=512)
        return any(n.id == candidate_id for n in chain)

    async def soft_delete_subtree(self, node_id: uuid.UUID, now: datetime) -> tuple[Node, ...]:
        targets = [
            self.by_id[node_id],
            *await self.descendants(node_id, max_depth=512, include_deleted=True),
        ]
        moved = [n for n in targets if not n.is_deleted]
        for node in moved:
            node.soft_delete(now)
        return tuple(moved)

    async def restore_subtree(self, node_id: uuid.UUID, now: datetime) -> tuple[Node, ...]:
        node = self.by_id.get(node_id)
        if node is None or node.deleted_at is None:
            return ()
        batch = node.deleted_at
        subtree = await self.descendants(node_id, max_depth=512, include_deleted=True)
        cleared = [node, *(n for n in subtree if n.deleted_at == batch)]
        for target in cleared:
            target.restore(now)
        return tuple(cleared)

    def _trash_entries(self, owner_id: uuid.UUID) -> list[Node]:
        """Trashed nodes of one owner that have a parent, and a live one.

        The collapse rule, kept faithful to the SQL `WHERE` clause: a unit test
        that passed here because the fake listed every trashed row would prove
        nothing about the listing users actually get. A root has no parent and is
        excluded for that reason, exactly as the SQL excludes it.
        """
        entries = []
        for node in self.by_id.values():
            if node.owner_id != owner_id or not node.is_deleted or node.parent_id is None:
                continue
            parent = self.by_id.get(node.parent_id)
            if parent is not None and parent.is_deleted:
                continue
            entries.append(node)
        return entries

    async def list_trash_entries(
        self,
        owner_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None = None,
        oldest_first: bool = False,
    ) -> Page[Node]:
        fingerprint = _trash_fingerprint(owner_id, oldest_first)
        ordered = sorted(self._trash_entries(owner_id), key=_trash_key, reverse=not oldest_first)
        if cursor is not None:
            edge = _trash_edge(cursor, fingerprint)
            ordered = [
                n
                for n in ordered
                if (_trash_key(n) > edge if oldest_first else _trash_key(n) < edge)
            ]
        has_more = len(ordered) > limit
        visible = ordered[:limit]
        return Page(
            items=tuple(visible),
            next_cursor=(_trash_cursor(visible[-1], fingerprint) if has_more and visible else None),
        )

    async def count_trash_entries(self, owner_id: uuid.UUID) -> int:
        return len(self._trash_entries(owner_id))

    async def delete_batch_totals(
        self, node_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, SubtreeTotals]:
        totals: dict[uuid.UUID, SubtreeTotals] = {}
        for node_id in node_ids:
            node = self.by_id.get(node_id)
            # A live node has no delete batch, so it is absent rather than zero --
            # the SQL aggregate drops it for the same reason (NULL = NULL).
            if node is None or node.deleted_at is None:
                continue
            # The same batch rule `restore_subtree` uses, so the reported total
            # is what a restore would actually lift.
            subtree = await self.descendants(node_id, max_depth=512, include_deleted=True)
            members = [node, *(n for n in subtree if n.deleted_at == node.deleted_at)]
            totals[node_id] = SubtreeTotals(
                size_bytes=sum(n.size_bytes for n in members if n.is_file),
                nodes=len(members),
            )
        return totals

    async def ancestor_chains(
        self, node_ids: Sequence[uuid.UUID], *, max_depth: int
    ) -> dict[uuid.UUID, tuple[Node, ...]]:
        return {node_id: await self.ancestors(node_id, max_depth=max_depth) for node_id in node_ids}

    async def list_trashed_before(self, cutoff: datetime, *, limit: int) -> tuple[Node, ...]:
        trashed = [
            n for n in self.by_id.values() if n.deleted_at is not None and n.deleted_at < cutoff
        ]
        return tuple(sorted(trashed, key=lambda n: n.deleted_at or cutoff)[:limit])

    async def delete_permanently(self, node_id: uuid.UUID) -> None:
        self.by_id.pop(node_id, None)
        self.tags.pop(node_id, None)
        self.metadata.pop(node_id, None)

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
        matches = []
        for node in self.by_id.values():
            if node.is_deleted:
                continue
            if term and term.lower() not in node.name.lower():
                continue
            if tags and not set(tags) <= self.tags.get(node.id, frozenset()):
                continue
            if key is not None:
                pairs = self.metadata.get(node.id, {})
                if key not in pairs or (value is not None and pairs[key] != value):
                    continue
            matches.append(node)
        return tuple(matches[:limit])

    async def tags_for(self, node_id: uuid.UUID) -> frozenset[str]:
        return self.tags.get(node_id, frozenset())

    async def replace_tags(self, node_id: uuid.UUID, tags: frozenset[str]) -> None:
        self.tags[node_id] = frozenset(tags)

    async def metadata_for(self, node_id: uuid.UUID) -> dict[str, str]:
        return dict(self.metadata.get(node_id, {}))

    async def replace_metadata(self, node_id: uuid.UUID, pairs: Mapping[str, str]) -> None:
        reserved = {
            key: value
            for key, value in self.metadata.get(node_id, {}).items()
            if key.casefold().startswith(RESERVED_METADATA_PREFIX)
        }
        self.metadata[node_id] = {**reserved, **pairs}

    # --- partial label updates ------------------------------------------
    #
    # Set arithmetic on a dict, which models the *result* of the row-level SQL
    # and nothing else. There is no unique constraint here, no advisory lock, and
    # no isolation, so `ON CONFLICT DO NOTHING`, the lost update these methods
    # exist to avoid, the per-node maximum holding under concurrency, and two
    # concurrent patches getting two distinct revisions are NOT provable against
    # this fake -- only against real Postgres, in tests/integration.

    async def add_tags(self, node_id: uuid.UUID, tags: frozenset[str]) -> None:
        self.tags[node_id] = self.tags.get(node_id, frozenset()) | tags

    async def remove_tags(self, node_id: uuid.UUID, tags: frozenset[str]) -> None:
        self.tags[node_id] = self.tags.get(node_id, frozenset()) - tags

    async def set_metadata(self, node_id: uuid.UUID, pairs: Mapping[str, str]) -> None:
        self.metadata.setdefault(node_id, {}).update(pairs)

    async def remove_metadata_keys(self, node_id: uuid.UUID, keys: frozenset[str]) -> None:
        current = self.metadata.get(node_id, {})
        self.metadata[node_id] = {k: v for k, v in current.items() if k not in keys}


class FakeKeyRepository:
    def __init__(self) -> None:
        self.user_keys: dict[uuid.UUID, UserKey] = {}
        self.data_keys: dict[tuple[uuid.UUID, str], WrappedDataKey] = {}

    async def get_user_key(self, user_id: uuid.UUID) -> UserKey | None:
        return self.user_keys.get(user_id)

    async def add_user_key(self, key: UserKey) -> None:
        self.user_keys[key.user_id] = key

    async def update_user_key(self, key: UserKey) -> None:
        self.user_keys[key.user_id] = key

    async def list_user_keys_wrapped_under(
        self, master_key_id: str, *, limit: int
    ) -> tuple[UserKey, ...]:
        return tuple(k for k in self.user_keys.values() if k.master_key_id == master_key_id)[:limit]

    async def get_data_key(self, node_id: uuid.UUID, subject: str) -> WrappedDataKey | None:
        return self.data_keys.get((node_id, subject))

    async def add_data_key(self, key: WrappedDataKey) -> None:
        self.data_keys[(key.node_id, key.subject)] = key

    async def list_data_keys(self, node_id: uuid.UUID) -> tuple[WrappedDataKey, ...]:
        return tuple(k for (n, _), k in self.data_keys.items() if n == node_id)

    async def delete_data_key(self, node_id: uuid.UUID, subject: str) -> None:
        self.data_keys.pop((node_id, subject), None)

    async def delete_data_keys_for_node(self, node_id: uuid.UUID) -> int:
        doomed = [key for key in self.data_keys if key[0] == node_id]
        for key in doomed:
            del self.data_keys[key]
        return len(doomed)


class FakeS3AccessKeyRepository:
    def __init__(self) -> None:
        self.by_key_id: dict[str, S3AccessKey] = {}

    async def get_by_key_id(self, key_id: str) -> S3AccessKey | None:
        return self.by_key_id.get(key_id)

    async def add(self, key: S3AccessKey) -> None:
        self.by_key_id[key.key_id] = key

    async def list_for_subject(self, owner_subject: str) -> tuple[S3AccessKey, ...]:
        owned = [k for k in self.by_key_id.values() if k.owner_subject == owner_subject]
        return tuple(sorted(owned, key=lambda k: k.created_at, reverse=True))

    async def update(self, key: S3AccessKey) -> None:
        self.by_key_id[key.key_id] = key


class FakeMultipartUploadRepository:
    def __init__(self) -> None:
        self.uploads: dict[str, MultipartUpload] = {}
        self.parts: dict[str, list[MultipartPart]] = {}

    async def create(self, upload: MultipartUpload) -> None:
        self.uploads[upload.upload_id] = upload
        self.parts.setdefault(upload.upload_id, [])

    async def get(self, upload_id: str) -> MultipartUpload | None:
        return self.uploads.get(upload_id)

    async def delete(self, upload_id: str) -> None:
        self.uploads.pop(upload_id, None)
        self.parts.pop(upload_id, None)

    async def add_part(self, part: MultipartPart) -> None:
        parts = self.parts.setdefault(part.upload_id, [])
        parts[:] = [p for p in parts if p.part_number != part.part_number]
        parts.append(part)

    async def list_parts(self, upload_id: str) -> tuple[MultipartPart, ...]:
        return tuple(sorted(self.parts.get(upload_id, []), key=lambda p: p.part_number))

    async def list_abandoned_before(
        self, cutoff: datetime, *, limit: int
    ) -> tuple[MultipartUpload, ...]:
        found = [u for u in self.uploads.values() if u.created_at < cutoff]
        return tuple(sorted(found, key=lambda u: u.created_at)[:limit])


class FakeFileVersionRepository:
    def __init__(self) -> None:
        self.by_id: dict[uuid.UUID, FileVersion] = {}

    async def get(self, version_id: uuid.UUID) -> FileVersion | None:
        return self.by_id.get(version_id)

    async def add(self, version: FileVersion) -> None:
        self.by_id[version.id] = version

    async def list_for_node(self, node_id: uuid.UUID) -> tuple[FileVersion, ...]:
        found = [v for v in self.by_id.values() if v.node_id == node_id]
        return tuple(sorted(found, key=lambda v: v.sequence, reverse=True))

    async def next_sequence(self, node_id: uuid.UUID) -> int:
        found = [v.sequence for v in self.by_id.values() if v.node_id == node_id]
        return max(found, default=0) + 1

    async def delete(self, version_id: uuid.UUID) -> None:
        self.by_id.pop(version_id, None)

    async def prune_beyond(self, node_id: uuid.UUID, keep: int) -> tuple[FileVersion, ...]:
        doomed = (await self.list_for_node(node_id))[keep:]
        for version in doomed:
            await self.delete(version.id)
        return doomed


class FakeGrantRepository:
    def __init__(self) -> None:
        self.by_id: dict[uuid.UUID, Grant] = {}

    async def get(self, grant_id: uuid.UUID) -> Grant | None:
        return self.by_id.get(grant_id)

    async def add(self, grant: Grant) -> None:
        self.by_id[grant.id] = grant

    async def update(self, grant: Grant) -> None:
        self.by_id[grant.id] = grant

    async def delete(self, grant_id: uuid.UUID) -> None:
        self.by_id.pop(grant_id, None)

    async def find(self, node_id: uuid.UUID, subject: str) -> Grant | None:
        return next(
            (g for g in self.by_id.values() if g.node_id == node_id and g.subject == subject),
            None,
        )

    async def list_for_node(self, node_id: uuid.UUID) -> tuple[Grant, ...]:
        # The owner-facing listing keeps pending grants so their status shows.
        return tuple(g for g in self.by_id.values() if g.node_id == node_id)

    async def list_for_subject(self, subject: str) -> tuple[Grant, ...]:
        # Active grants only, mirroring the SQL repository.
        return tuple(g for g in self.by_id.values() if g.subject == subject and not g.pending)

    async def highest_role_over(self, subject: str, node_ids: Sequence[uuid.UUID]) -> Role | None:
        scope = set(node_ids)
        roles = [
            g.role
            for g in self.by_id.values()
            if g.subject == subject and g.node_id in scope and not g.pending
        ]
        return max(roles, default=None)

    async def list_pending(self, *, limit: int) -> tuple[Grant, ...]:
        pending = sorted((g for g in self.by_id.values() if g.pending), key=lambda g: g.created_at)
        return tuple(pending[:limit])

    async def delete_for_node(self, node_id: uuid.UUID) -> int:
        doomed = [g.id for g in self.by_id.values() if g.node_id == node_id]
        for grant_id in doomed:
            del self.by_id[grant_id]
        return len(doomed)


class FakePublicLinkRepository:
    def __init__(self) -> None:
        self.by_id: dict[uuid.UUID, PublicLink] = {}

    async def get(self, link_id: uuid.UUID) -> PublicLink | None:
        return self.by_id.get(link_id)

    async def get_by_token_hash(self, token_hash: str) -> PublicLink | None:
        return next((link for link in self.by_id.values() if link.token_hash == token_hash), None)

    async def add(self, link: PublicLink) -> None:
        self.by_id[link.id] = link

    async def update(self, link: PublicLink) -> None:
        self.by_id[link.id] = link

    async def list_for_node(self, node_id: uuid.UUID) -> tuple[PublicLink, ...]:
        return tuple(link for link in self.by_id.values() if link.node_id == node_id)

    async def list_active(self, *, limit: int, cursor: str | None = None) -> Page[PublicLink]:
        active = [link for link in self.by_id.values() if not link.is_revoked]
        return Page(items=tuple(active[:limit]))


class FakeQuotaRepository:
    """Recomputes from the node rows, so reconciliation tests are meaningful."""

    def __init__(self, nodes: FakeNodeRepository | None = None) -> None:
        self.by_user: dict[uuid.UUID, QuotaUsage] = {}
        self._nodes = nodes

    async def get(self, user_id: uuid.UUID) -> QuotaUsage | None:
        return self.by_user.get(user_id)

    async def add(self, usage: QuotaUsage) -> None:
        self.by_user[usage.user_id] = usage

    async def update(self, usage: QuotaUsage) -> None:
        self.by_user[usage.user_id] = usage

    async def recompute(self, user_id: uuid.UUID) -> QuotaUsage:
        if self._nodes is None:
            return QuotaUsage(user_id=user_id)
        owned = [n for n in self._nodes.by_id.values() if n.owner_id == user_id and n.is_file]
        return QuotaUsage(
            user_id=user_id,
            live_bytes=sum(n.size_bytes for n in owned if not n.is_deleted),
            trashed_bytes=sum(n.size_bytes for n in owned if n.is_deleted),
        )


class FakeAuditRepository:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        #: When set, `add` raises, so the non-blocking-audit path is testable.
        self.fail_add = False

    async def add(self, record: AuditRecord) -> None:
        if self.fail_add:
            raise RuntimeError("audit store is unavailable")
        self.records.append(record)

    async def query(self, **_: object) -> Page[AuditRecord]:
        return Page(items=tuple(self.records))

    async def prune_activity(self, cutoff: datetime, *, actions: Sequence[str]) -> int:
        wanted = set(actions)
        doomed = [r for r in self.records if str(r.action) in wanted and r.occurred_at < cutoff]
        for record in doomed:
            self.records.remove(record)
        return len(doomed)


class FakeActivityQueries:
    """In-memory activity aggregates over the audit records.

    Honors the same rules the SQL adapter does: self-scoping by actor, the
    window bound, action filtering, newest-first order, and a name only when
    the calling user owns the node (purged and cross-user nodes stay id-only).
    """

    def __init__(
        self,
        audit: FakeAuditRepository,
        nodes: FakeNodeRepository,
        users: FakeUserRepository,
    ) -> None:
        self._audit = audit
        self._nodes = nodes
        self._users = users

    def _in_window(self, actor_subject: str, since: datetime, until: datetime) -> list[AuditRecord]:
        return [
            r
            for r in self._audit.records
            if r.actor_subject == actor_subject and since <= r.occurred_at < until
        ]

    async def summary(
        self, *, actor_subject: str, since: datetime, until: datetime
    ) -> ActivityRollup:
        counts = [
            ActivityCount(
                action=r.action,
                day=r.occurred_at.date(),
                count=1,
                bytes=int(r.context.get("bytes", 0) or 0),
            )
            for r in self._in_window(actor_subject, since, until)
        ]
        return build_rollup(counts, window_start=since, window_end=until)

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
        rows = self._in_window(actor_subject, since, until)
        if action is not None:
            rows = [r for r in rows if str(r.action) == action]
        rows.sort(key=lambda r: r.occurred_at, reverse=True)
        if cursor is not None:
            edge = datetime.fromisoformat(base64.urlsafe_b64decode(cursor.encode()).decode())
            rows = [r for r in rows if r.occurred_at < edge]

        has_more = len(rows) > limit
        visible = rows[:limit]
        actor = await self._users.get_by_subject(actor_subject)
        actor_id = actor.id if actor is not None else None
        entries = tuple(self._to_entry(r, actor_id) for r in visible)
        next_cursor = (
            base64.urlsafe_b64encode(visible[-1].occurred_at.isoformat().encode()).decode()
            if has_more and visible
            else None
        )
        return Page(items=entries, next_cursor=next_cursor)

    def _to_entry(self, record: AuditRecord, actor_id: uuid.UUID | None) -> ActivityEntry:
        name = self._owned_name(record.target_id, actor_id)
        return ActivityEntry(
            action=record.action,
            occurred_at=record.occurred_at,
            node_id=record.target_id,
            node_name=name,
            protocol=record.protocol or AuditProtocol.REST,
        )

    def _owned_name(self, target_id: str | None, actor_id: uuid.UUID | None) -> str | None:
        if target_id is None or actor_id is None:
            return None
        try:
            node = self._nodes.by_id.get(uuid.UUID(target_id))
        except ValueError:
            return None
        if node is None or node.owner_id != actor_id:
            return None
        return node.name


class FakeUnitOfWork:
    """Records commit/rollback so tests can assert transaction boundaries."""

    def __init__(self) -> None:
        self.users = FakeUserRepository()
        self.nodes = FakeNodeRepository()
        self.versions = FakeFileVersionRepository()
        self.grants = FakeGrantRepository()
        self.public_links = FakePublicLinkRepository()
        self.keys = FakeKeyRepository()
        self.s3_keys = FakeS3AccessKeyRepository()
        self.multipart = FakeMultipartUploadRepository()
        self.quotas = FakeQuotaRepository(self.nodes)
        self.audit = FakeAuditRepository()
        self.activity = FakeActivityQueries(self.audit, self.nodes, self.users)
        self.committed = 0
        self.rolled_back = 0
        self.flushed = 0

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            await self.rollback()

    async def flush(self) -> None:
        self.flushed += 1

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def lock_subtree(self, node_id: uuid.UUID) -> None:
        return None


class FakeKeyProvider:
    """Deterministic stand-in for the real envelope crypto.

    Wrapping is a reversible transformation, not encryption -- enough to assert
    that only sealed material is ever persisted, without pulling real AEAD into
    a unit test.
    """

    def __init__(self, master_key_id: str = "master-test") -> None:
        self._master_key_id = master_key_id
        self.generated = 0

    @property
    def master_key_id(self) -> str:
        return self._master_key_id

    def generate_kek(self) -> bytes:
        self.generated += 1
        return f"kek-{self.generated}".encode()

    def wrap_kek(self, kek: bytes) -> bytes:
        return b"wrapped:" + kek

    def unwrap_kek(self, wrapped: bytes, *, master_key_id: str) -> bytes:
        return wrapped.removeprefix(b"wrapped:")

    def seal_secret(self, data: bytes) -> bytes:
        # Reversible transform, not encryption -- enough to prove that only
        # sealed material is ever persisted, without pulling real AEAD into a
        # unit test. The sealed form is deliberately unequal to the plaintext.
        return b"sealed:" + data

    def unseal_secret(self, sealed: bytes, *, master_key_id: str) -> bytes:
        return sealed.removeprefix(b"sealed:")


class FakeObjectStore:
    """In-memory object store implementing the `ObjectStore` port."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.timestamps: dict[str, datetime] = {}
        self.deleted: list[str] = []

    async def put(
        self,
        key: str,
        data: AsyncIterator[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> int:
        buffer = bytearray()
        async for chunk in data:
            buffer.extend(chunk)
        self.objects[key] = bytes(buffer)
        return len(buffer)

    async def get(
        self, key: str, *, offset: int = 0, length: int | None = None
    ) -> AsyncIterator[bytes]:
        blob = self.objects.get(key)
        if blob is None:
            raise NotFoundError("stored object is missing", key=key)
        end = offset + length if length is not None else len(blob)
        # Chunked deliberately, so callers cannot assume one whole read.
        window = blob[offset:end]
        for start in range(0, len(window), 8):
            yield window[start : start + 8]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted.append(key)

    async def copy(self, source_key: str, target_key: str) -> int:
        blob = self.objects.get(source_key)
        if blob is None:
            raise NotFoundError("source object is missing", key=source_key)
        self.objects[target_key] = blob
        return len(blob)

    async def stat(self, key: str) -> int | None:
        blob = self.objects.get(key)
        return len(blob) if blob is not None else None

    async def list_keys(self, prefix: str = "") -> AsyncIterator[StoredObject]:
        for key, blob in list(self.objects.items()):
            if key.startswith(prefix):
                yield StoredObject(key=key, size=len(blob), last_modified=self.timestamps.get(key))

    async def ensure_bucket(self) -> None:
        return None


async def stream(payload: bytes, chunk: int = 8) -> AsyncIterator[bytes]:
    """Feed bytes in small pieces, as a real request body would arrive."""
    for start in range(0, len(payload), chunk):
        yield payload[start : start + chunk]
