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
    TrashCountMismatchError,
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
    TRASH_PURGE_NODE_BUDGET,
    EncryptionDefault,
    Node,
    NodeKind,
    NodePath,
    SubtreeTotals,
    TrashEntry,
    normalize_name,
    validate_metadata,
    validate_name,
    validate_tags,
)
from cyberfs.domain.pagination import decode_cursor, decode_keyed_cursor
from cyberfs.domain.permissions import resolve_effective_role
from cyberfs.domain.ports.repositories import Page, UnitOfWork
from cyberfs.domain.ports.storage import ObjectStore
from cyberfs.domain.s3.namespace import SHARED_PREFIX
from cyberfs.domain.search import SearchFilters, TagFilters, TagMatch, TagUsage
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


@dataclass(frozen=True, slots=True)
class TrashListing:
    """A page of the caller's trash, plus how many entries it holds in total.

    The total travels with the page because it is the input to the empty-trash
    guard, not a UI nicety: obtaining it from a second endpoint would let the two
    numbers disagree, and paginating a thousand-entry trash to derive it would
    make a guard nobody can satisfy on a first call.
    """

    entries: tuple[TrashEntry, ...]
    next_cursor: str | None
    total_entries: int


@dataclass(frozen=True, slots=True)
class Emptied:
    """What one bounded pass over the trash destroyed, and what it left behind.

    `entries_remaining` is read back after the destruction rather than subtracted
    from the count that was confirmed, so a client looping on it is following the
    trash as it actually stands.
    """

    entries_purged: int
    entries_remaining: int
    purged: Purged


class NodeService:
    def __init__(
        self,
        *,
        max_tree_depth: int,
        page_size_max: int,
        # Only the trash deadline reads this, so it carries the same default as
        # `Settings.trash_retention_days` rather than being restated at the many
        # construction sites that never touch the trash. `create_app` always
        # passes the configured value, and a test pins that it does.
        trash_retention_days: int = 30,
        cache: CacheService | None = None,
    ) -> None:
        self._max_depth = max_tree_depth
        self._page_size_max = page_size_max
        self._retention_days = trash_retention_days
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
        tag_match: TagMatch = TagMatch.ALL,
        limit: int,
        cursor: str | None = None,
    ) -> Page[Node]:
        """Metadata only. Content is never indexed, so it can never be matched.

        Every filter narrows, and the filter set is built once here so that the
        fingerprint a cursor carries and the fingerprint a request implies come
        from the same code path. The cursor is read here too, for the same
        reason: the repository is handed a decoded sort key and never sees a
        token. The access scope is resolved in the query, not here -- there is no
        per-node authorization to do, because nothing outside the scope is ever
        returned to compare against.
        """
        filters = SearchFilters.of(term=term, tags=tags, match=tag_match, key=key, value=value)
        return await uow.nodes.search(
            user.subject,
            filters,
            limit=min(limit, self._page_size_max),
            after=_search_key(cursor, filters.fingerprint),
        )

    async def tag_inventory(
        self,
        uow: UnitOfWork,
        user: User,
        *,
        prefix: str | None = None,
        limit: int,
        cursor: str | None = None,
    ) -> Page[TagUsage]:
        """The caller's own tag vocabulary, so a UI can offer it back to them.

        Authenticated only: like `search`, the scope is the query's, and the
        counts it reports are the caller's own -- never a property of the tag.
        Nothing here writes, so nothing here invalidates a cache.
        """
        filters = TagFilters.of(prefix)
        after: str | None = None
        if cursor is not None:
            (after,) = decode_keyed_cursor(
                decode_cursor(cursor), fingerprint=filters.fingerprint, fields=1
            )
        return await uow.nodes.tag_counts(
            user.subject,
            filters,
            limit=min(limit, self._page_size_max),
            after=after,
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
        await self._invalidate_subtree(node, cleared)
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
        total = await self._destroy_entry(uow, objects, node, moment)
        await uow.flush()
        await self._record_purge(uow, user, node, total, moment)
        await self._invalidate(node.id, old_parent=node.parent_id, reparented=True)
        return total

    # --- the trash -----------------------------------------------------

    async def trash(
        self,
        uow: UnitOfWork,
        user: User,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> TrashListing:
        """The caller's own trash, most recently deleted first.

        Owner-scoped through the repository's signature: a soft delete withdraws
        every grant, so there is no caller other than the owner for whom an entry
        could exist, and no parameter by which another user's trash could be
        asked for.

        Not cached. Invalidating a trash listing would mean reacting to every
        delete, restore, and purge anywhere in the caller's tree, and a stale
        trash fails in both available directions: an entry that is missing cannot
        be restored, and one that is already gone `404`s when the user clicks it.
        """
        page = await uow.nodes.list_trash_entries(
            user.id, limit=min(limit, self._page_size_max), cursor=cursor
        )
        ids = [node.id for node in page.items]
        # Two page-wide queries rather than two per entry. A folder's own
        # `size_bytes` is zero, and "0 bytes" beside every deleted folder
        # withholds the single number the user needs in order to choose between
        # restoring and purging; the path needs the live ancestors above it.
        totals = await uow.nodes.delete_batch_totals(ids)
        chains = await uow.nodes.ancestor_chains(ids, max_depth=ANCESTOR_GUARD_DEPTH)
        entries = tuple(
            TrashEntry.of(
                node,
                chains.get(node.id, ()),
                retention_days=self._retention_days,
                totals=totals.get(node.id, SubtreeTotals()),
            )
            for node in page.items
        )
        return TrashListing(
            entries=entries,
            next_cursor=page.next_cursor,
            total_entries=await uow.nodes.count_trash_entries(user.id),
        )

    async def empty_trash(
        self,
        uow: UnitOfWork,
        user: User,
        *,
        expected_entries: int,
        objects: ObjectStore,
        now: datetime | None = None,
    ) -> Emptied:
        """Destroy the caller's own trash entries. Irreversible.

        Guarded by the count the caller states: a trash holding a different
        number is refused and nothing is destroyed. A `confirm` flag would be a
        constant no client could get wrong, which is exactly why it would be no
        evidence that the caller looked; the count can only be right if they
        listed the trash, and it goes stale the moment something changes.

        The count and the destruction share one unit of work, so a node trashed
        concurrently either lands before the count -- and is destroyed -- or after
        the commit, and is reported as remaining. Never half of either.
        """
        moment = now or utcnow()
        held = await uow.nodes.count_trash_entries(user.id)
        if held != expected_entries:
            raise TrashCountMismatchError(
                "the trash does not hold the stated number of entries; list it again",
                expected_entries=expected_entries,
                entries=held,
            )

        destroyed, total = await self._spend_purge_budget(uow, user, objects, moment)
        await uow.flush()
        remaining = await uow.nodes.count_trash_entries(user.id)
        if destroyed:
            # A no-op call writes no record. `TRASH_EMPTIED` is deliberately
            # outside `ACTIVITY_ACTIONS`, so nothing ever prunes it -- emitting
            # one per request would let any authenticated client grow the
            # permanently retained security log with rows describing nothing.
            await self._record_batch(uow, user, moment, entries=len(destroyed), purged=total)
        await self._invalidate_destroyed(destroyed)
        return Emptied(entries_purged=len(destroyed), entries_remaining=remaining, purged=total)

    async def _spend_purge_budget(
        self,
        uow: UnitOfWork,
        user: User,
        objects: ObjectStore,
        now: datetime,
    ) -> tuple[list[Node], Purged]:
        """Destroy whole entries, oldest first, up to `TRASH_PURGE_NODE_BUDGET`.

        The budget counts NODES, because an entry is the root of a subtree of
        unbounded size and bounding entries would bound nothing. An entry whose
        node count would push the call past the budget is not *started*: a
        half-destroyed entry would still list, with a subtree no longer matching
        its reported totals, which is worse than one not yet touched.

        One exception, or the loop never terminates: with nothing destroyed yet
        the oldest entry goes however large it is. Otherwise a trash whose oldest
        entry exceeds the budget could never be emptied by any sequence of calls.
        That leaves a single deep subtree costing exactly what
        `POST /nodes/{id}/purge` already costs for it.
        """
        # Every entry costs at least its own row, so no call can destroy more
        # entries than the node budget allows -- which is what bounds this read.
        page = await uow.nodes.list_trash_entries(
            user.id, limit=TRASH_PURGE_NODE_BUDGET, oldest_first=True
        )
        totals = await uow.nodes.delete_batch_totals([node.id for node in page.items])
        destroyed: list[Node] = []
        total = Purged()
        spent = 0
        for entry in page.items:
            cost = totals[entry.id].nodes
            if destroyed and spent + cost > TRASH_PURGE_NODE_BUDGET:
                break
            purged = await self._destroy_entry(uow, objects, entry, now)
            # One record per entry, identical in shape to what an individual
            # purge of it writes -- not one per node, which would flood the
            # never-pruned security log with rows naming descendants no user
            # ever addressed.
            await self._record_purge(uow, user, entry, purged, now)
            destroyed.append(entry)
            spent += cost
            total += purged
        return destroyed, total

    async def _destroy_entry(
        self,
        uow: UnitOfWork,
        objects: ObjectStore,
        entry: Node,
        now: datetime,
    ) -> Purged:
        """One trashed node and its subtree, destroyed. Shared by both purges.

        `purge_subtree` owns the order -- every node stripped of its objects
        before any row goes, because `NodeRow.parent_id` cascades and would take a
        descendant's row before its object key had been used. Re-deriving that
        sequence per caller is how a stranded object or a quota leak arrives.
        """
        subtree = await uow.nodes.descendants(
            entry.id, max_depth=ANCESTOR_GUARD_DEPTH, include_deleted=True
        )
        return await purge_subtree(
            uow, objects, [*(d.id for d in subtree), entry.id], entry.id, now
        )

    @staticmethod
    async def _record_purge(
        uow: UnitOfWork,
        user: User,
        node: Node,
        purged: Purged,
        now: datetime,
    ) -> None:
        """The one `NODE_PURGED` emitter, shared by `purge` and `empty_trash`.

        `purge_one` and `purge_subtree` emit nothing, deliberately: their other
        caller is the retention sweep, which has no actor to attribute. So the
        record is written here, and written once -- two emitters would let the
        granularity of a purge's audit trail depend on which route reached it.
        """
        await emit_audit(
            uow,
            AuditRecord(
                action=AuditAction.NODE_PURGED,
                occurred_at=now,
                actor_subject=user.subject,
                target_id=str(node.id),
                context={
                    **owner_context(node, user.id),
                    # Named separately so an administrator's purge of someone
                    # else's node stays attributable to both parties.
                    "owner_id": str(node.owner_id),
                    "nodes": purged.nodes_deleted,
                    "objects": purged.objects_deleted,
                    "bytes": purged.bytes_reclaimed,
                },
            ),
        )

    @staticmethod
    async def _record_batch(
        uow: UnitOfWork,
        user: User,
        now: datetime,
        *,
        entries: int,
        purged: Purged,
    ) -> None:
        """One record naming the batch, so a sudden drop in usage is explained.

        Carries no node identifier: the batch is not a node, and the per-entry
        `node.purged` records already name every one of them.
        """
        await emit_audit(
            uow,
            AuditRecord(
                action=AuditAction.TRASH_EMPTIED,
                occurred_at=now,
                actor_subject=user.subject,
                context={
                    "entries": entries,
                    "nodes": purged.nodes_deleted,
                    "objects": purged.objects_deleted,
                    "bytes": purged.bytes_reclaimed,
                },
            ),
        )

    async def _invalidate_destroyed(self, entries: Sequence[Node]) -> None:
        """Drop what the destruction made stale, before the response."""
        if self._cache is None or not entries:
            return
        for entry in entries:
            await self._cache.on_node_mutated(entry.id, old_parent=entry.parent_id)
        # The subtrees are gone, so every decision inherited over them is void.
        await self._cache.invalidate_all_permissions()

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

    async def _invalidate_subtree(self, entry: Node, lifted: Sequence[Node]) -> None:
        """A subtree mutation, so every row it touched is dropped, not just the root.

        `caching/spec.md` "Invalidation on mutation" requires a subtree mutation to
        drop the cached decisions and listings of descendants too, and a restore
        mutates every row it lifts. `_invalidate` alone would drop the entry's node
        key and its *parent's* listing prefix while leaving the descendants' node
        keys and the entry's own children listing behind. Nothing populates those
        datasets on read today, which is exactly why this is fixed now rather than
        when a read path starts trusting them.
        """
        await self._invalidate(entry.id, old_parent=entry.parent_id, reparented=True)
        if self._cache is None:
            return
        for node in lifted:
            await self._cache.invalidate_node(node.id)
        await self._cache.invalidate_listing(entry.id)

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


def _search_key(cursor: str | None, fingerprint: str) -> tuple[str, str] | None:
    """The `(id, normalized_name)` a search cursor names, or nothing.

    Read here rather than in the repository so the refusals -- a mangled token, a
    token issued for other filters, a well-signed token naming something that is
    not an identifier -- are raised by code every adapter shares and a unit test
    can reach without a database.
    """
    if cursor is None:
        return None
    node_id, name = decode_keyed_cursor(decode_cursor(cursor), fingerprint=fingerprint, fields=2)
    try:
        uuid.UUID(node_id)
    except ValueError as exc:
        raise ValidationError("cursor is not valid") from exc
    return node_id, name


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
