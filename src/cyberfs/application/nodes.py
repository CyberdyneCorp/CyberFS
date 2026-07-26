"""Filesystem use cases: the tree, without content.

Every operation resolves the caller's effective permission first, then acts.
Content upload and download live in the storage capability; what happens here
is purely structural -- create, list, rename, move, copy, trash, search.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cyberfs.application.auditing import authorize_or_record, emit_audit, owner_context
from cyberfs.application.caching import CacheService
from cyberfs.application.purge import Purged, purge_one
from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import (
    ConflictError,
    CrossOwnerMoveError,
    NameTakenError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
    WouldCreateCycleError,
)
from cyberfs.domain.nodes import (
    EncryptionDefault,
    Node,
    NodeKind,
    NodePath,
    normalize_name,
    validate_name,
)
from cyberfs.domain.permissions import resolve_effective_role
from cyberfs.domain.ports.repositories import Page, UnitOfWork
from cyberfs.domain.ports.storage import ObjectStore
from cyberfs.domain.s3.namespace import SHARED_PREFIX
from cyberfs.domain.sharing import Role
from cyberfs.domain.users import User

#: Recursion ceiling for ancestor walks. Well above any legitimate tree, so a
#: corrupted parent link is caught rather than mistaken for a deep hierarchy.
ANCESTOR_GUARD_DEPTH = 512


@dataclass(frozen=True, slots=True)
class NodeView:
    """A node as a caller sees it, including their own permission on it."""

    node: Node
    role: Role
    path: str
    parent_id: uuid.UUID | None

    @property
    def etag(self) -> str:
        return self.node.etag


class NodeService:
    def __init__(
        self,
        *,
        max_tree_depth: int,
        page_size_max: int,
        cache: CacheService | None = None,
    ) -> None:
        self._max_depth = max_tree_depth
        self._page_size_max = page_size_max
        self._cache = cache

    # --- permission ----------------------------------------------------

    async def effective_role(
        self, uow: UnitOfWork, subject: str, owner_id: uuid.UUID, node: Node
    ) -> Role | None:
        """The caller's role on `node`, folding in grants on it and its ancestors.

        The ancestor walk is the one genuinely hot query in the system, which
        is why it is cached -- and why every grant change invalidates the
        caller's decisions synchronously rather than waiting for a TTL.
        """

        async def compute() -> Role | None:
            chain = await uow.nodes.ancestors(node.id, max_depth=ANCESTOR_GUARD_DEPTH)
            scope = [node.id, *(a.id for a in chain)]
            granted = await uow.grants.highest_role_over(subject, scope)
            return resolve_effective_role(
                is_owner=node.owner_id == owner_id,
                granted=(granted,) if granted is not None else (),
            )

        if self._cache is None:
            return await compute()
        return await self._cache.permission(subject, node.id, compute)

    async def _authorize(
        self, uow: UnitOfWork, user: User, node_id: uuid.UUID, minimum: Role
    ) -> tuple[Node, Role]:
        node = await uow.nodes.get(node_id)
        if node is None or node.is_deleted:
            # Same response as "no permission", so a probe cannot distinguish
            # a missing node from one the caller may not see.
            raise NotFoundError("node not found", node_id=str(node_id))
        role = await self.effective_role(uow, user.subject, user.id, node)
        granted = await authorize_or_record(
            uow,
            actor_subject=user.subject,
            node_id=node_id,
            effective=role,
            minimum=minimum,
        )
        return node, granted

    # --- reads ---------------------------------------------------------

    async def get(self, uow: UnitOfWork, user: User, node_id: uuid.UUID) -> NodeView:
        node, role = await self._authorize(uow, user, node_id, Role.VIEWER)
        return await self._view(uow, node, role)

    async def list_children(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> Page[Node]:
        node, _ = await self._authorize(uow, user, node_id, Role.VIEWER)
        if not node.is_folder:
            raise ValidationError("only a folder has children", node_id=str(node_id))
        return await uow.nodes.list_children(
            node.id, limit=min(limit, self._page_size_max), cursor=cursor
        )

    async def search(
        self, uow: UnitOfWork, user: User, term: str, *, limit: int
    ) -> tuple[Node, ...]:
        """Metadata only. Content is never indexed, so it can never be matched."""
        if not term.strip():
            raise ValidationError("a search term is required")
        return await uow.nodes.search_by_name(
            user.subject, term.strip(), limit=min(limit, self._page_size_max)
        )

    async def _view(self, uow: UnitOfWork, node: Node, role: Role) -> NodeView:
        chain = await uow.nodes.ancestors(node.id, max_depth=ANCESTOR_GUARD_DEPTH)
        path = NodePath(node, chain)
        return NodeView(node=node, role=role, path=path.path, parent_id=node.parent_id)

    # --- writes --------------------------------------------------------

    @staticmethod
    async def _audit(
        uow: UnitOfWork, user: User, node: Node, action: AuditAction, when: datetime
    ) -> None:
        """Emit one operation record, non-blocking, name only when owned."""
        await emit_audit(
            uow,
            AuditRecord(
                action=action,
                occurred_at=when,
                actor_subject=user.subject,
                target_id=str(node.id),
                context=owner_context(node, user.id),
            ),
        )

    async def create_folder(
        self,
        uow: UnitOfWork,
        user: User,
        parent_id: uuid.UUID,
        name: str,
        *,
        encryption_default: EncryptionDefault = EncryptionDefault.INHERIT,
        now: datetime | None = None,
    ) -> NodeView:
        moment = now or utcnow()
        parent, _ = await self._authorize(uow, user, parent_id, Role.EDITOR)
        await self._ensure_folder(parent)
        _ensure_not_reserved_root_name(parent, name)
        await self._ensure_depth(uow, parent)

        node = Node(
            id=uuid.uuid4(),
            # Charged to whoever owns the parent, not to the creator: an editor
            # working in someone else's folder does not pay for it.
            owner_id=parent.owner_id,
            kind=NodeKind.FOLDER,
            name=validate_name(name),
            parent_id=parent.id,
            created_at=moment,
            updated_at=moment,
            encryption_default=encryption_default,
        )
        await self._ensure_name_free(uow, parent.id, node.normalized_name)
        await uow.nodes.add(node)
        await uow.flush()
        await self._audit(uow, user, node, AuditAction.NODE_CREATED, moment)
        await self._invalidate(node.id, old_parent=parent.id)
        return await self._view(uow, node, Role.OWNER if node.owner_id == user.id else Role.EDITOR)

    async def rename(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        name: str,
        *,
        if_match: str | None = None,
        now: datetime | None = None,
    ) -> NodeView:
        moment = now or utcnow()
        node, role = await self._authorize(uow, user, node_id, Role.EDITOR)
        _ensure_precondition(node, if_match)
        if node.is_root:
            raise ValidationError("a root folder cannot be renamed")

        candidate = validate_name(name)
        if node.parent_id is not None:
            await self._ensure_name_free(
                uow, node.parent_id, normalize_name(candidate), excluding=node.id
            )
        node.rename(candidate, moment)
        await uow.nodes.update(node)
        await uow.flush()
        await self._audit(uow, user, node, AuditAction.NODE_RENAMED, moment)
        await self._invalidate(node.id, old_parent=node.parent_id)
        return await self._view(uow, node, role)

    async def move(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        new_parent_id: uuid.UUID,
        *,
        if_match: str | None = None,
        now: datetime | None = None,
    ) -> NodeView:
        moment = now or utcnow()
        # Serialize on the destination: two concurrent moves could each pass a
        # cycle check individually and still create one together.
        await uow.lock_subtree(new_parent_id)

        node, role = await self._authorize(uow, user, node_id, Role.EDITOR)
        _ensure_precondition(node, if_match)
        destination, _ = await self._authorize(uow, user, new_parent_id, Role.EDITOR)
        await self._ensure_folder(destination)

        if node.is_root:
            raise ValidationError("a root folder cannot be moved")
        if destination.owner_id != node.owner_id:
            raise CrossOwnerMoveError(
                "move would cross an ownership boundary",
                node_id=str(node_id),
            )
        if await uow.nodes.is_ancestor_of(node.id, destination.id):
            raise WouldCreateCycleError(node_id=str(node_id))
        await self._ensure_depth(uow, destination)

        await self._ensure_name_free(uow, destination.id, node.normalized_name, excluding=node.id)
        previous_parent = node.parent_id
        node.move_to(destination.id, moment)
        await uow.nodes.update(node)
        await uow.flush()
        await self._audit(uow, user, node, AuditAction.NODE_MOVED, moment)
        # A move changes inherited access for everyone with a grant above the
        # old or new location, so every permission decision is dropped.
        await self._invalidate(
            node.id, old_parent=previous_parent, new_parent=destination.id, reparented=True
        )
        return await self._view(uow, node, role)

    async def delete(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        *,
        if_match: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Soft-delete a node and everything beneath it. Returns how many."""
        moment = now or utcnow()
        node, _ = await self._authorize(uow, user, node_id, Role.OWNER)
        _ensure_precondition(node, if_match)
        if node.is_root:
            raise ValidationError("a root folder cannot be deleted")

        # Measured before the delete, while the subtree is still visible.
        bytes_trashed = await self._subtree_bytes(uow, node)
        count = await uow.nodes.soft_delete_subtree(node.id, moment)
        # The bytes are still stored, so they still count -- they just move
        # from live to trashed until a purge actually frees them.
        await self._move_bytes(uow, node.owner_id, bytes_trashed, moment, to_trash=True)
        await uow.flush()
        await self._audit(uow, user, node, AuditAction.NODE_DELETED, moment)
        await self._invalidate(node.id, old_parent=node.parent_id, reparented=True)
        return count

    async def restore(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        *,
        now: datetime | None = None,
    ) -> NodeView:
        """Bring a trashed node back, into its old parent or into the root."""
        moment = now or utcnow()
        node = await uow.nodes.get(node_id)
        if node is None or not node.is_deleted:
            raise NotFoundError("no trashed node with that id", node_id=str(node_id))
        # Only the owner sees their trash; grants were dropped on delete.
        await authorize_or_record(
            uow,
            actor_subject=user.subject,
            node_id=node_id,
            effective=resolve_effective_role(is_owner=node.owner_id == user.id),
            minimum=Role.OWNER,
        )

        parent = await uow.nodes.get(node.parent_id) if node.parent_id else None
        if parent is None or parent.is_deleted:
            # The original home is gone; the root always exists.
            node.parent_id = user.root_folder_id
        destination = node.parent_id
        assert destination is not None, "a restored node always has a parent"
        await self._ensure_name_free(uow, destination, node.normalized_name, excluding=node.id)

        node.restore(moment)
        await uow.nodes.update(node)
        restored = await self._subtree_bytes(uow, node)
        await self._move_bytes(uow, node.owner_id, restored, moment, to_trash=False)
        await uow.flush()
        await self._audit(uow, user, node, AuditAction.NODE_RESTORED, moment)
        await self._invalidate(node.id, old_parent=node.parent_id, reparented=True)
        return await self._view(uow, node, Role.OWNER)

    async def purge(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        *,
        objects: ObjectStore,
        now: datetime | None = None,
    ) -> Purged:
        """Destroy a trashed node and its subtree. Irreversible.

        Requires the node to be in the trash already, so losing content takes
        two deliberate steps. The owner may purge their own; an administrator
        may purge anyone's, and the quota released is always the owner's.
        """
        moment = now or utcnow()
        node = await uow.nodes.get(node_id)
        if node is None:
            raise NotFoundError("node not found", node_id=str(node_id))

        # Authorize before disclosing whether the node is trashed or live, and
        # record the denial rather than only refusing it.
        if node.owner_id != user.id and not user.is_admin:
            await authorize_or_record(
                uow,
                actor_subject=user.subject,
                node_id=node_id,
                effective=resolve_effective_role(is_owner=False),
                minimum=Role.OWNER,
            )

        if not node.is_deleted:
            raise ConflictError(
                "only a node in the trash can be purged; delete it first",
                node_id=str(node_id),
            )

        # Descendants first. Deleting the root's row cascades to their rows, so
        # purging it before them would leave their objects in the store with no
        # metadata pointing at them, and undercount the quota released.
        subtree = await uow.nodes.descendants(
            node.id, max_depth=ANCESTOR_GUARD_DEPTH, include_deleted=True
        )
        total = Purged()
        for descendant in subtree:
            total += await purge_one(uow, objects, descendant.id, moment)
        total += await purge_one(uow, objects, node.id, moment)

        await uow.flush()
        await emit_audit(
            uow,
            AuditRecord(
                action=AuditAction.NODE_PURGED,
                occurred_at=moment,
                actor_subject=user.subject,
                target_id=str(node.id),
                context={
                    **owner_context(node, user.id),
                    # Named separately so an administrator's purge of someone
                    # else's node stays attributable to both parties.
                    "owner_id": str(node.owner_id),
                    "nodes": total.nodes_deleted,
                    "objects": total.objects_deleted,
                    "bytes": total.bytes_reclaimed,
                },
            ),
        )
        await self._invalidate(node.id, old_parent=node.parent_id, reparented=True)
        return total

    async def copy(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        destination_id: uuid.UUID,
        *,
        name: str | None = None,
        content: ContentDuplicator | None = None,
        now: datetime | None = None,
    ) -> NodeView:
        """Duplicate a file or a folder subtree into `destination_id`.

        The copy belongs to the caller and is charged to them, and it carries
        no grants -- `sharing/spec.md` is explicit that a copy is visible only
        to its new owner, so a copy can never be a way to launder access.
        """
        moment = now or utcnow()
        source, _ = await self._authorize(uow, user, node_id, Role.VIEWER)
        destination, _ = await self._authorize(uow, user, destination_id, Role.EDITOR)
        await self._ensure_folder(destination)
        if await uow.nodes.is_ancestor_of(source.id, destination.id):
            raise WouldCreateCycleError(
                "a folder cannot be copied into itself", node_id=str(node_id)
            )
        await self._ensure_depth(uow, destination)

        root_copy = await self._copy_one(
            uow, user, source, destination.id, moment, name=name, content=content
        )
        if source.is_folder:
            await self._copy_subtree(uow, user, source, root_copy, moment, content)
        await uow.flush()
        # The audited fact is "the user copied X" -- the root copy, not each
        # child individually. A copy is always owned by the caller, so its name
        # is present.
        await self._audit(uow, user, root_copy, AuditAction.NODE_COPIED, moment)
        return await self._view(uow, root_copy, Role.OWNER)

    async def _copy_subtree(
        self,
        uow: UnitOfWork,
        user: User,
        source: Node,
        target: Node,
        now: datetime,
        content: ContentDuplicator | None,
    ) -> None:
        """Breadth-first, so a deep tree costs stack depth of one."""
        pending = [(source, target)]
        while pending:
            original, copied = pending.pop()
            page = await uow.nodes.list_children(original.id, limit=self._page_size_max)
            for child in page.items:
                child_copy = await self._copy_one(uow, user, child, copied.id, now, content=content)
                if child.is_folder:
                    pending.append((child, child_copy))

    async def _copy_one(
        self,
        uow: UnitOfWork,
        user: User,
        source: Node,
        parent_id: uuid.UUID,
        now: datetime,
        *,
        name: str | None = None,
        content: ContentDuplicator | None = None,
    ) -> Node:
        chosen = validate_name(name) if name is not None else source.name
        copy = Node(
            id=uuid.uuid4(),
            owner_id=user.id,
            kind=source.kind,
            name=chosen,
            parent_id=parent_id,
            created_at=now,
            updated_at=now,
            content_type=source.content_type,
            encrypted=source.encrypted,
            encryption_default=source.encryption_default,
        )
        await self._ensure_name_free(uow, parent_id, copy.normalized_name)
        await uow.nodes.add(copy)
        await uow.flush()

        if source.is_file:
            copy.size_bytes = await self._duplicate_content(uow, source, copy, now, content)
            await self._charge(uow, user, copy.size_bytes, now)
            await uow.nodes.update(copy)
        return copy

    @staticmethod
    async def _duplicate_content(
        uow: UnitOfWork,
        source: Node,
        target: Node,
        now: datetime,
        content: ContentDuplicator | None,
    ) -> int:
        if content is None:
            # No duplicator wired (a metadata-only test double); the copy has
            # no content rather than content nobody can reach.
            return 0
        return await content.duplicate(uow, source, target, now)

    @staticmethod
    async def _charge(uow: UnitOfWork, user: User, size_bytes: int, now: datetime) -> None:
        """A copy is new storage, charged to whoever made it."""
        if size_bytes <= 0:
            return
        usage = await uow.quotas.get(user.id)
        if usage is None:
            return
        usage.ensure_room_for(user.quota_bytes, size_bytes)
        usage.charge_live(size_bytes, now)
        await uow.quotas.update(usage)

    @staticmethod
    async def _subtree_bytes(uow: UnitOfWork, node: Node) -> int:
        """Total content bytes in a node and everything beneath it."""
        subtree = await uow.nodes.descendants(
            node.id, max_depth=ANCESTOR_GUARD_DEPTH, include_deleted=True
        )
        return sum(n.size_bytes for n in (node, *subtree) if n.is_file)

    @staticmethod
    async def _move_bytes(
        uow: UnitOfWork,
        owner_id: uuid.UUID,
        size_bytes: int,
        now: datetime,
        *,
        to_trash: bool,
    ) -> None:
        if size_bytes <= 0:
            return
        usage = await uow.quotas.get(owner_id)
        if usage is None:
            return
        if to_trash:
            usage.move_to_trash(size_bytes, now)
        else:
            usage.restore_from_trash(size_bytes, now)
        await uow.quotas.update(usage)

    async def _invalidate(
        self,
        node_id: uuid.UUID,
        *,
        old_parent: uuid.UUID | None = None,
        new_parent: uuid.UUID | None = None,
        reparented: bool = False,
    ) -> None:
        """Drop everything this mutation can have made stale.

        Synchronous and before the response: correctness must never depend on
        a TTL expiring.
        """
        if self._cache is None:
            return
        await self._cache.on_node_mutated(node_id, old_parent=old_parent, new_parent=new_parent)
        if reparented:
            # Inherited access moved with the subtree.
            await self._cache.invalidate_all_permissions()

    # --- guards --------------------------------------------------------

    @staticmethod
    async def _ensure_folder(node: Node) -> None:
        if not node.is_folder:
            raise ValidationError("target must be a folder", node_id=str(node.id))

    async def _ensure_depth(self, uow: UnitOfWork, parent: Node) -> None:
        chain = await uow.nodes.ancestors(parent.id, max_depth=ANCESTOR_GUARD_DEPTH)
        if len(chain) + 1 >= self._max_depth:
            raise ValidationError(
                f"tree depth would exceed {self._max_depth}", node_id=str(parent.id)
            )

    @staticmethod
    async def _ensure_name_free(
        uow: UnitOfWork,
        parent_id: uuid.UUID,
        normalized_name: str,
        *,
        excluding: uuid.UUID | None = None,
    ) -> None:
        """Pre-check for a friendly error.

        The database's partial unique index is the actual guarantee -- this
        check can be raced -- so the violation is also translated at commit.
        """
        existing = await uow.nodes.get_child_by_name(parent_id, normalized_name)
        if existing is not None and existing.id != excluding:
            raise NameTakenError("a sibling already uses that name", parent_id=str(parent_id))


def _ensure_not_reserved_root_name(parent: Node, name: str) -> None:
    """Keep `shared` free at the root of every tree.

    The S3 namespace presents nodes shared *with* the caller under a reserved
    ``shared/<owner>/…`` prefix; a real folder named `shared` at the root would
    shadow that view (`s3-compatibility/spec.md`, "The reserved prefix cannot be
    shadowed"). The name stays legal deeper in the tree -- only the root is
    reserved -- so the guard is scoped to a root parent.
    """
    if parent.is_root and normalize_name(name) == SHARED_PREFIX:
        raise ValidationError(
            f"{SHARED_PREFIX!r} is reserved at the root of a tree", parent_id=str(parent.id)
        )


def _ensure_precondition(node: Node, if_match: str | None) -> None:
    """Reject a stale update, so a lost write is detectable by the client."""
    if if_match is None:
        return
    supplied = {tag.strip() for tag in if_match.split(",")}
    if "*" in supplied or node.etag in supplied:
        return
    raise PreconditionFailedError(
        "the node has changed since it was read",
        node_id=str(node.id),
        etag=node.etag,
    )


class ContentDuplicator(Protocol):
    """Copies the content behind a file.

    Structural copying -- the tree, ownership, and quota -- is this module's
    job. Duplicating the stored object *and the version row that names it*
    belongs to the storage capability, so both arrive through this port: a copy
    with bytes but no version would be a file nobody can download.
    """

    async def duplicate(self, uow: UnitOfWork, source: Node, target: Node, now: datetime) -> int:
        """Copy content from `source` to `target`; returns bytes copied."""
        ...
