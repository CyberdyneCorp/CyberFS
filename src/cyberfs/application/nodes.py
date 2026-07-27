"""Filesystem use cases: the tree, without content.

Every operation resolves the caller's effective permission first, then acts.
Content upload and download live in the storage capability; what happens here
is purely structural -- create, list, rename, move, copy, trash, search.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cyberfs.application.auditing import authorize_or_record, emit_audit, owner_context
from cyberfs.application.caching import CacheService
from cyberfs.application.purge import Purged, purge_subtree
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
from cyberfs.domain.labels import (
    is_reserved_key,
    merge_metadata,
    merge_tags,
    metadata_change_counts,
    tag_change_counts,
    validate_metadata_delta,
    validate_tag_delta,
    visible_metadata,
)
from cyberfs.domain.nodes import (
    MAX_METADATA_PAIRS,
    EncryptionDefault,
    Node,
    NodeKind,
    NodePath,
    normalize_name,
    normalize_tag,
    validate_metadata,
    validate_name,
    validate_tags,
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
        self,
        uow: UnitOfWork,
        user: User,
        term: str | None = None,
        *,
        tags: Sequence[str] = (),
        key: str | None = None,
        value: str | None = None,
        limit: int,
    ) -> tuple[Node, ...]:
        """Metadata only. Content is never indexed, so it can never be matched.

        Every filter narrows. At least one is required: an unfiltered search
        would be a listing of everything the caller can reach, which is what the
        tree walk is for.
        """
        cleaned = (term or "").strip()
        normalized = [normalize_tag(t) for t in tags if normalize_tag(t)]
        if value is not None and key is None:
            raise ValidationError("a metadata value needs the key it belongs to")
        if not cleaned and not normalized and key is None:
            raise ValidationError("a search needs a name, a tag, or a metadata key")
        return await uow.nodes.search(
            user.subject,
            term=cleaned or None,
            tags=normalized,
            key=key,
            value=value,
            limit=min(limit, self._page_size_max),
        )

    # --- labels --------------------------------------------------------

    async def replace_tags(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        tags: Iterable[str],
        *,
        if_match: str | None = None,
        now: datetime | None = None,
    ) -> tuple[NodeView, frozenset[str]]:
        """Replace a node's tags wholesale. Requires `EDITOR`, like renaming.

        Serialized against partial updates on the same node. A replace states a
        complete collection, so it has no merge to lose -- but a `PATCH` deciding
        the per-node maximum from a collection this replace is in the middle of
        rewriting would decide it from a state that never existed, which is what
        turns that maximum back into an advisory.
        """
        moment = now or utcnow()
        validated = validate_tags(tags)
        await uow.lock_subtree(node_id)

        node, role = await self._authorize(uow, user, node_id, Role.EDITOR)
        _ensure_precondition(node, if_match)

        await uow.nodes.replace_tags(node.id, validated)
        node.touch(moment)
        await uow.nodes.update(node)
        await uow.flush()
        await self._audit(uow, user, node, AuditAction.NODE_TAGS_CHANGED, moment)
        await self._invalidate(node.id, old_parent=node.parent_id)
        return await self._view(uow, node, role), validated

    async def replace_metadata(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        pairs: Sequence[tuple[str, str]],
        *,
        if_match: str | None = None,
        now: datetime | None = None,
    ) -> tuple[NodeView, dict[str, str]]:
        """Replace a node's metadata wholesale. Requires `EDITOR`.

        Serialized against partial updates for the reason `replace_tags` gives.
        """
        moment = now or utcnow()
        validated = validate_metadata(pairs)
        await uow.lock_subtree(node_id)

        node, role = await self._authorize(uow, user, node_id, Role.EDITOR)
        _ensure_precondition(node, if_match)
        # The repository preserves reserved rows through a replace, so the
        # caller's pairs are not the whole collection the node ends up carrying.
        # Validating only the request would let a replace seat
        # `MAX_METADATA_PAIRS` pairs on top of them, leaving the node over a
        # maximum a `PATCH` enforces exactly -- the same constant meaning two
        # different things depending on the verb.
        reserved = sum(1 for key in await uow.nodes.metadata_for(node.id) if is_reserved_key(key))
        if reserved + len(validated) > MAX_METADATA_PAIRS:
            raise ValidationError(
                f"a node may carry at most {MAX_METADATA_PAIRS} metadata pairs, "
                f"and {reserved} of them are already reserved for CyberFS"
            )

        await uow.nodes.replace_metadata(node.id, validated)
        node.touch(moment)
        await uow.nodes.update(node)
        await uow.flush()
        await self._audit(uow, user, node, AuditAction.NODE_METADATA_CHANGED, moment)
        await self._invalidate(node.id, old_parent=node.parent_id)
        return await self._view(uow, node, role), validated

    async def patch_tags(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
        if_match: str | None = None,
        now: datetime | None = None,
    ) -> tuple[NodeView, frozenset[str]]:
        """Merge a tag delta into what the node already carries. Requires `EDITOR`.

        Unlike `replace_tags`, this states a change rather than a collection: no
        statement touches a tag the request did not name, so a patch cannot
        clobber a label it never saw.
        """
        moment = now or utcnow()
        # Validated first: it is pure, and a body that was never going to be
        # accepted should not reach the point of taking a lock.
        delta = validate_tag_delta(add, remove)
        # Then the lock, before the node is read. The per-node maximum and the
        # "changed nothing" judgement are both decided from the current
        # collection, and an unserialized read of it is the lost update the
        # row-level writes below otherwise avoid.
        #
        # It cannot move after `_authorize` -- which would keep a caller with no
        # rights on the node from taking the lock at all -- because the session's
        # identity map would then serve the pre-lock row to every read that
        # follows, and the serialization would be silently worthless. See
        # `design.md`, "Why the lock precedes authorization".
        await uow.lock_subtree(node_id)

        node, role = await self._authorize(uow, user, node_id, Role.EDITOR)
        # Before the effect is computed. A stale token says the caller's view of
        # the node is out of date, which is true whether or not the delta would
        # have changed anything.
        _ensure_precondition(node, if_match)

        current = await uow.nodes.tags_for(node.id)
        merged = merge_tags(current, delta)
        if merged == current:
            # A patch that changes nothing writes nothing: bumping the revision
            # would invalidate every other client's ETag for a change none of
            # them can observe.
            return await self._view(uow, node, role), current

        # The delta as the caller named it, not the difference from what was
        # read: a writer that does not hold this lock -- a `PUT`, or any label
        # writer added later -- must not lose rows to a patch that never named
        # them.
        await uow.nodes.add_tags(node.id, delta.added)
        await uow.nodes.remove_tags(node.id, delta.removed)
        added, removed = tag_change_counts(current, merged)
        await self._apply_label_change(
            uow, user, node, AuditAction.NODE_TAGS_CHANGED, moment, added=added, removed=removed
        )
        return await self._view(uow, node, role), merged

    async def patch_metadata(
        self,
        uow: UnitOfWork,
        user: User,
        node_id: uuid.UUID,
        *,
        pairs: Sequence[tuple[str, str]] = (),
        remove: Iterable[str] = (),
        if_match: str | None = None,
        now: datetime | None = None,
    ) -> tuple[NodeView, dict[str, str]]:
        """Set and delete individual metadata keys. Requires `EDITOR`.

        Every key the request does not name is left byte-identical, including
        anything in the reserved namespace -- which a removal may not name at all,
        and which the returned mapping does not show.
        """
        moment = now or utcnow()
        delta = validate_metadata_delta(pairs, remove)
        await uow.lock_subtree(node_id)

        node, role = await self._authorize(uow, user, node_id, Role.EDITOR)
        _ensure_precondition(node, if_match)

        # Unfiltered, so a reserved pair still counts towards the maximum; only
        # what is handed back is filtered.
        current = await uow.nodes.metadata_for(node.id)
        merged = merge_metadata(current, delta)
        if merged == current:
            return await self._view(uow, node, role), visible_metadata(current)

        await uow.nodes.set_metadata(node.id, delta.pairs)
        await uow.nodes.remove_metadata_keys(node.id, delta.removed)
        written, removed = metadata_change_counts(current, merged)
        await self._apply_label_change(
            uow,
            user,
            node,
            AuditAction.NODE_METADATA_CHANGED,
            moment,
            added=written,
            removed=removed,
        )
        return await self._view(uow, node, role), visible_metadata(merged)

    async def _apply_label_change(
        self,
        uow: UnitOfWork,
        user: User,
        node: Node,
        action: AuditAction,
        when: datetime,
        *,
        added: int,
        removed: int,
    ) -> None:
        """Everything a partial update owes the rest of the system once it bites.

        The revision is bumped in Python, exactly as the replace does, which is
        safe here only because the node was read under the lock: the second patch
        reads what the first committed. A SQL increment would leave this `Node` --
        and therefore the ETag on the response -- on the pre-patch revision.
        """
        node.touch(when)
        await uow.nodes.update(node)
        await uow.flush()
        await emit_audit(
            uow,
            AuditRecord(
                action=action,
                occurred_at=when,
                actor_subject=user.subject,
                target_id=str(node.id),
                # Counts, never the tag or key text: activity is pruned on a
                # different clock from the labels it would be quoting.
                context={**owner_context(node, user.id), "added": added, "removed": removed},
            ),
        )
        await self._invalidate(node.id, old_parent=node.parent_id)

    async def labels_for(
        self, uow: UnitOfWork, node_id: uuid.UUID
    ) -> tuple[frozenset[str], dict[str, str]]:
        """A node's tags and metadata, for callers already authorized to read it.

        Reserved pairs are withheld, so the metadata a caller is handed is exactly
        the metadata it may write back -- a `PUT` of what a `GET` returned must not
        be refused for echoing a key CyberFS wrote.
        """
        tags = await uow.nodes.tags_for(node_id)
        return tags, visible_metadata(await uow.nodes.metadata_for(node_id))

    @staticmethod
    async def current_digest(uow: UnitOfWork, node: Node) -> str | None:
        """The plaintext digest of a node's current version, if it has one.

        Only ever handed to a caller who may read the content -- they can
        download the bytes and hash them anyway, so it tells them nothing new.
        It stays off the administrative surface, where it would.
        """
        if node.current_version_id is None:
            return None
        version = await uow.versions.get(node.current_version_id)
        return version.plaintext_digest if version is not None else None

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

        trashed = await uow.nodes.soft_delete_subtree(node.id, moment)
        count = len(trashed)
        # Only the rows that actually moved are charged. Summing the whole
        # subtree would charge a descendant already in the trash a second time,
        # leaving the buckets disagreeing with the rows until the reconcile job
        # noticed -- the mirror image of the restore bug this accompanies.
        bytes_trashed = sum(n.size_bytes for n in trashed if n.is_file)
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
        if parent is not None and not parent.is_deleted:
            destination = parent.id
        else:
            # The original home is gone; the root always exists.
            destination = user.root_folder_id
        await self._ensure_name_free(uow, destination, node.normalized_name, excluding=node.id)

        # The repository clears the rows first: the node's own `deleted_at` is
        # what identifies the batch it went to the trash with, so nothing here
        # may touch it beforehand.
        cleared = await uow.nodes.restore_subtree(node.id, moment)
        # Re-read, so the new parent is stamped on top of the row the repository
        # just wrote rather than under a copy that still looks trashed.
        node = await uow.nodes.get(node_id)
        assert node is not None, "the subtree was cleared, not removed"
        node.parent_id = destination
        await uow.nodes.update(node)
        # Only the rows that actually came back leave the trashed bucket. A
        # descendant trashed on an earlier occasion stays there, bytes and all,
        # so the buckets keep matching what the rows say.
        restored = sum(n.size_bytes for n in cleared if n.is_file)
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

        # Every node is stripped of its objects before any row is deleted.
        # `NodeRow.parent_id` cascades, so deleting rows during the walk would
        # let a descendant's row vanish before its object key had been used --
        # stranding the object in the store and undercounting the quota freed.
        subtree = await uow.nodes.descendants(
            node.id, max_depth=ANCESTOR_GUARD_DEPTH, include_deleted=True
        )
        total = await purge_subtree(
            uow, objects, [*(d.id for d in subtree), node.id], node.id, moment
        )

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
