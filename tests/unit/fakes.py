"""In-memory implementations of the repository ports.

Let use cases be tested as pure logic, with no Postgres. The SQLAlchemy
implementations of the same ports are covered by the integration suite, which
is where real constraint and transaction behaviour actually lives.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import TracebackType

from cyberfs.domain.audit import AuditRecord
from cyberfs.domain.keys import UserKey, WrappedDataKey
from cyberfs.domain.nodes import Node
from cyberfs.domain.ports.repositories import Page
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


class FakeNodeRepository:
    def __init__(self) -> None:
        self.by_id: dict[uuid.UUID, Node] = {}

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

    async def soft_delete_subtree(self, node_id: uuid.UUID, now: datetime) -> int:
        targets = [self.by_id[node_id], *await self.descendants(node_id, max_depth=512)]
        count = 0
        for node in targets:
            if not node.is_deleted:
                node.soft_delete(now)
                count += 1
        return count

    async def list_trashed_before(self, cutoff: datetime, *, limit: int) -> tuple[Node, ...]:
        trashed = [
            n for n in self.by_id.values() if n.deleted_at is not None and n.deleted_at < cutoff
        ]
        return tuple(sorted(trashed, key=lambda n: n.deleted_at or cutoff)[:limit])

    async def delete_permanently(self, node_id: uuid.UUID) -> None:
        self.by_id.pop(node_id, None)

    async def search_by_name(self, subject: str, term: str, *, limit: int) -> tuple[Node, ...]:
        matches = [
            n for n in self.by_id.values() if term.lower() in n.name.lower() and not n.is_deleted
        ]
        return tuple(matches[:limit])


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


class FakeQuotaRepository:
    def __init__(self) -> None:
        self.by_user: dict[uuid.UUID, QuotaUsage] = {}

    async def get(self, user_id: uuid.UUID) -> QuotaUsage | None:
        return self.by_user.get(user_id)

    async def add(self, usage: QuotaUsage) -> None:
        self.by_user[usage.user_id] = usage

    async def update(self, usage: QuotaUsage) -> None:
        self.by_user[usage.user_id] = usage

    async def recompute(self, user_id: uuid.UUID) -> QuotaUsage:
        return self.by_user.get(user_id) or QuotaUsage(user_id=user_id)


class FakeAuditRepository:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def add(self, record: AuditRecord) -> None:
        self.records.append(record)

    async def query(self, **_: object) -> Page[AuditRecord]:
        return Page(items=tuple(self.records))


class FakeUnitOfWork:
    """Records commit/rollback so tests can assert transaction boundaries."""

    def __init__(self) -> None:
        self.users = FakeUserRepository()
        self.nodes = FakeNodeRepository()
        self.keys = FakeKeyRepository()
        self.quotas = FakeQuotaRepository()
        self.audit = FakeAuditRepository()
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
