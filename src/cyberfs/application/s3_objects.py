"""S3 object orchestration over the existing filesystem use cases.

The S3 surface addresses nodes by *path*; the REST surface addresses them by
id. Translating a key to a node -- walking the tree segment by segment, and
resolving the reserved ``shared/<owner>/…`` view against the grant repository --
is genuinely new logic, so it lives here in the application layer where it can
compose repository reads with the permission checks the content and node
services already enforce.

What this service deliberately does *not* do is re-implement storage, quota,
encryption, or trash behaviour: every read is delegated to `ContentService`
(which authorizes, decrypts, and honours `Range`) and every listing size is the
recorded plaintext size, so the S3 view is identical to REST by construction
rather than by a parallel implementation.

The authorized view is enforced by reachability, not by a second permission
layer: in the caller's own tree every node under their root is theirs; under the
shared prefix only nodes beneath a root they hold a grant on are reachable at
all. A node they may not see is simply never walked to, so it is `NoSuchKey`,
identical to one that never existed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from cyberfs.application.content import ContentService, DownloadPlan
from cyberfs.application.nodes import ANCESTOR_GUARD_DEPTH, NodeService
from cyberfs.domain.errors import (
    CyberFSError,
    NoSuchBucketError,
    NoSuchKeyError,
    ValidationError,
)
from cyberfs.domain.nodes import Node, NodePath, normalize_name
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.s3.listing import ListRequest, ListResult, S3ObjectEntry, list_objects_v2
from cyberfs.domain.s3.namespace import (
    SHARED_PREFIX,
    S3Target,
    bucket_for_subject,
    key_for,
    resolve_key,
    subject_for_bucket,
)
from cyberfs.domain.users import User

DEFAULT_CONTENT_TYPE = "application/octet-stream"

#: The reserved prefix as a key. When the requested prefix is a prefix *of* this
#: -- ``""``, ``"s"``, … ``"shared/"`` -- a listing must also surface the shared
#: view, so ``shared/`` appears as a `CommonPrefix` at the bucket root.
_SHARED_ROOT_KEY = f"{SHARED_PREFIX}/"


@dataclass(frozen=True, slots=True)
class BucketInfo:
    """A bucket as `ListBuckets` reports it -- one per caller, named for them."""

    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class HeadInfo:
    """The metadata `HeadObject` returns: all plaintext, no storage detail."""

    size: int
    etag: str
    last_modified: datetime
    content_type: str


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    """One key's fate in a `DeleteObjects` batch -- deleted, or why not.

    S3 reports each key independently, so a batch never fails wholesale: a key
    the caller may not delete carries its own `<Error>` while its siblings are
    still trashed.
    """

    key: str
    error_code: str | None = None
    error_message: str | None = None

    @property
    def deleted(self) -> bool:
        return self.error_code is None


class S3ObjectService:
    def __init__(
        self,
        nodes: NodeService,
        content: ContentService,
        *,
        page_size_max: int,
    ) -> None:
        # `nodes` is used by the write path (phase 7); the read path reaches the
        # tree through the unit of work directly and decrypts through `content`.
        self._nodes = nodes
        self._content = content
        self._page_size_max = page_size_max

    # --- buckets -------------------------------------------------------

    def list_buckets(self, user: User) -> tuple[BucketInfo, ...]:
        """Exactly the caller's own bucket -- bucket names are not a directory."""
        return (BucketInfo(name=bucket_for_subject(user.subject), created_at=user.created_at),)

    def head_bucket(self, user: User, bucket: str) -> None:
        """Confirm the caller's own bucket; any other subject is `NoSuchBucket`."""
        self._require_own_bucket(user, bucket)

    # --- objects -------------------------------------------------------

    async def resolve_object(self, uow: UnitOfWork, user: User, target: S3Target) -> Node:
        """Map a resolved key to the file it names, or raise `NoSuchKey`.

        The walk maps names to ids only; permission is a property of
        reachability. A folder-shaped key is `NoSuchKey`, since S3 objects are
        files.
        """
        segments = _segments(target.path)
        if target.via_shared:
            node = await self._resolve_shared(uow, user, target, segments)
        else:
            node = await self._descend(uow, user.root_folder_id, segments)
        if node is None or node.is_folder:
            raise NoSuchKeyError("no such key", key=target.path)
        return node

    async def head_object(self, uow: UnitOfWork, user: User, target: S3Target) -> HeadInfo:
        """Metadata for a reachable object -- plaintext size, never storage size."""
        node = await self.resolve_object(uow, user, target)
        return HeadInfo(
            size=node.size_bytes,
            etag=node.etag,
            last_modified=node.updated_at,
            content_type=node.content_type or DEFAULT_CONTENT_TYPE,
        )

    async def get_object(
        self, uow: UnitOfWork, user: User, target: S3Target, *, range_header: str | None = None
    ) -> DownloadPlan:
        """Stream an object, delegating decryption and `Range` to `ContentService`."""
        node = await self.resolve_object(uow, user, target)
        return await self._content.download(uow, user, node.id, range_header=range_header)

    # --- writes --------------------------------------------------------

    async def put_object(
        self,
        uow: UnitOfWork,
        user: User,
        target: S3Target,
        body: AsyncIterator[bytes],
        *,
        content_type: str | None = None,
        declared_length: int | None = None,
    ) -> Node:
        """Store an object through the existing upload use case.

        Everything the REST upload enforces applies unchanged, because the same
        `ContentService.upload` runs it: quota (`QuotaExceeded`, storing nothing
        on rejection), versioning of an existing key, and encryption inheritance
        from the folder. Intermediate folders on the key path are created as
        needed. A write under the shared prefix by a viewer-only caller is
        refused with `AccessDenied`, since `upload` requires `EDITOR` on the
        parent -- there is no parallel permission check here.
        """
        parent_segments, name = _split_key(target.path)
        start_id = await self._write_root(uow, target)
        parent_id = await self._ensure_parents(uow, user, start_id, parent_segments)
        return await self._content.upload(
            uow,
            user,
            parent_id,
            name,
            body,
            content_type=content_type,
            declared_length=declared_length,
        )

    async def copy_object(
        self, uow: UnitOfWork, user: User, source: S3Target, destination: S3Target
    ) -> Node:
        """Duplicate one key to another server-side, carrying no grants.

        `NodeService.copy` does the work: the content is duplicated through the
        object store without transiting the client, the copy is owned by and
        charged to the caller, and it inherits none of the source's grants.
        """
        src_node = await self.resolve_object(uow, user, source)
        parent_segments, name = _split_key(destination.path)
        start_id = await self._write_root(uow, destination)
        parent_id = await self._ensure_parents(uow, user, start_id, parent_segments)
        view = await self._nodes.copy(
            uow, user, src_node.id, parent_id, name=name, content=self._content
        )
        return view.node

    async def delete_object(self, uow: UnitOfWork, user: User, target: S3Target) -> None:
        """Soft-delete the object a key names, recoverable for the trash window.

        `DeleteObject` is idempotent in S3, so a key that names nothing is a
        success rather than a `NoSuchKey`. Deletion of a real object is a
        `NodeService.delete` -- a soft delete to trash, never a destroy.
        """
        try:
            node = await self.resolve_object(uow, user, target)
        except NoSuchKeyError:
            return
        await self._nodes.delete(uow, user, node.id)

    async def delete_objects(
        self, uow: UnitOfWork, user: User, bucket: str, keys: Sequence[str]
    ) -> tuple[DeleteOutcome, ...]:
        """Delete a batch, reporting each key's fate independently."""
        return tuple([await self._delete_one(uow, user, bucket, key) for key in keys])

    async def _delete_one(
        self, uow: UnitOfWork, user: User, bucket: str, key: str
    ) -> DeleteOutcome:
        try:
            target = resolve_key(user.subject, bucket, key)
            await self.delete_object(uow, user, target)
            return DeleteOutcome(key=key)
        except CyberFSError as exc:
            code = getattr(exc, "s3_code", None) or "AccessDenied"
            return DeleteOutcome(key=key, error_code=code, error_message=exc.message)

    async def _write_root(self, uow: UnitOfWork, target: S3Target) -> uuid.UUID:
        """The folder a key's path descends from -- the tree of its owner.

        A key in the caller's own tree names the caller as owner, so this is
        their root; a key under the shared prefix names the other subject, so it
        descends *their* tree. Either way the grant the caller holds is enforced
        by the delegated `create_folder`/`upload`, never here: descending is
        pure navigation, and every mutation on the path authorizes itself.
        """
        owner = await uow.users.get_by_subject(target.owner_subject)
        if owner is None:
            raise NoSuchKeyError("no such key", key=target.path)
        return owner.root_folder_id

    async def _ensure_parents(
        self, uow: UnitOfWork, user: User, start_id: uuid.UUID, segments: tuple[str, ...]
    ) -> uuid.UUID:
        """Descend the folder path, creating any missing folder as we go."""
        current = start_id
        for segment in segments:
            current = await self._child_or_create(uow, user, current, segment)
        return current

    async def _child_or_create(
        self, uow: UnitOfWork, user: User, parent_id: uuid.UUID, name: str
    ) -> uuid.UUID:
        existing = await uow.nodes.get_child_by_name(parent_id, normalize_name(name))
        if existing is not None:
            if existing.is_file:
                raise ValidationError("a file already occupies the key path", key=name)
            return existing.id
        view = await self._nodes.create_folder(uow, user, parent_id, name)
        return view.node.id

    # --- listing -------------------------------------------------------

    async def list_objects(
        self, uow: UnitOfWork, user: User, bucket: str, req: ListRequest
    ) -> ListResult:
        """List the caller's authorized view: their own tree plus the shared view."""
        self._require_own_bucket(user, bucket)
        prefix_segments = _segments(req.prefix)
        if prefix_segments and prefix_segments[0] == SHARED_PREFIX:
            entries = await self._gather_shared(uow, user)
        else:
            entries = await self._gather_own(uow, user, req.prefix)
            if _SHARED_ROOT_KEY.startswith(req.prefix):
                entries.extend(await self._gather_shared(uow, user))
        return list_objects_v2(entries, req)

    # --- own-tree gather ----------------------------------------------

    async def _gather_own(self, uow: UnitOfWork, user: User, prefix: str) -> list[S3ObjectEntry]:
        """Every file the caller owns beneath the folder the prefix addresses."""
        dir_segments = _dir_segments(prefix)
        base = await self._descend(uow, user.root_folder_id, dir_segments)
        if base is None or base.is_file:
            return []
        base_key = "".join(f"{segment}/" for segment in dir_segments)
        return await self._walk(uow, base.id, base_key)

    async def _walk(
        self, uow: UnitOfWork, folder_id: uuid.UUID, key_prefix: str
    ) -> list[S3ObjectEntry]:
        """Depth-first over a subtree, emitting files keyed by their full path.

        Keys are accumulated on the way down, so no node needs a separate
        ancestor walk. Trashed nodes never appear -- `list_children` excludes
        them -- and the recorded `size_bytes` is the plaintext size.
        """
        entries: list[S3ObjectEntry] = []
        stack: list[tuple[uuid.UUID, str]] = [(folder_id, key_prefix)]
        while stack:
            current_id, prefix = stack.pop()
            async for child in self._children(uow, current_id):
                child_key = prefix + child.name
                if child.is_file:
                    entries.append(_entry(child_key, child))
                else:
                    stack.append((child.id, f"{child_key}/"))
        return entries

    async def _children(self, uow: UnitOfWork, folder_id: uuid.UUID) -> AsyncIterator[Node]:
        cursor: str | None = None
        while True:
            page = await uow.nodes.list_children(
                folder_id, limit=self._page_size_max, cursor=cursor
            )
            for child in page.items:
                yield child
            cursor = page.next_cursor
            if cursor is None:
                return

    # --- shared-view gather -------------------------------------------

    async def _gather_shared(self, uow: UnitOfWork, user: User) -> list[S3ObjectEntry]:
        """Every file under a node shared with the caller, keyed under `shared/`."""
        entries: list[S3ObjectEntry] = []
        for root in await self._shared_roots(uow, user):
            owner = await uow.users.get(root.owner_id)
            if owner is None:
                continue
            base_path = await self._path_of(uow, root)
            base_key = key_for(user.subject, owner.subject, base_path)
            if root.is_file:
                entries.append(_entry(base_key, root))
            else:
                entries.extend(await self._walk(uow, root.id, f"{base_key}/"))
        return entries

    async def _resolve_shared(
        self, uow: UnitOfWork, user: User, target: S3Target, segments: tuple[str, ...]
    ) -> Node | None:
        """Descend into the shared subtree the target addresses, if reachable."""
        for root in await self._shared_roots(uow, user):
            owner = await uow.users.get(root.owner_id)
            if owner is None or owner.subject != target.owner_subject:
                continue
            base = _segments(await self._path_of(uow, root))
            if segments[: len(base)] != base:
                continue
            remaining = segments[len(base) :]
            return root if not remaining else await self._descend(uow, root.id, remaining)
        return None

    async def _shared_roots(self, uow: UnitOfWork, user: User) -> tuple[Node, ...]:
        """The roots of each shared subtree -- not every inherited descendant.

        Mirrors `SharingService.shared_with_me`: a grant covered by another grant
        further up the tree is dropped, so a subtree is listed once.
        """
        grants = await uow.grants.list_for_subject(user.subject)
        granted_ids = {grant.node_id for grant in grants}
        roots: list[Node] = []
        for grant in grants:
            node = await uow.nodes.get(grant.node_id)
            if node is None or node.is_deleted:
                continue
            chain = await uow.nodes.ancestors(node.id, max_depth=ANCESTOR_GUARD_DEPTH)
            if any(ancestor.id in granted_ids for ancestor in chain):
                continue
            roots.append(node)
        return tuple(roots)

    # --- helpers -------------------------------------------------------

    async def _descend(
        self, uow: UnitOfWork, start_id: uuid.UUID, segments: tuple[str, ...]
    ) -> Node | None:
        """Follow a path of normalized names from a folder, or None if broken.

        `get_child_by_name` excludes deleted rows, so a trashed node on the path
        makes the key unreachable -- exactly the trash exclusion S3 requires.
        """
        current = await uow.nodes.get(start_id)
        for segment in segments:
            if current is None or not current.is_folder:
                return None
            current = await uow.nodes.get_child_by_name(current.id, normalize_name(segment))
        return current

    @staticmethod
    async def _path_of(uow: UnitOfWork, node: Node) -> str:
        chain = await uow.nodes.ancestors(node.id, max_depth=ANCESTOR_GUARD_DEPTH)
        return NodePath(node, chain).path

    @staticmethod
    def _require_own_bucket(user: User, bucket: str) -> None:
        if subject_for_bucket(bucket) != user.subject:
            raise NoSuchBucketError("no such bucket", bucket=bucket)


def _entry(key: str, node: Node) -> S3ObjectEntry:
    """One object entry -- plaintext size and the derived ETag, never a key."""
    return S3ObjectEntry(
        key=key,
        size=node.size_bytes,
        etag=node.etag,
        last_modified=node.updated_at,
    )


def _segments(key: str) -> tuple[str, ...]:
    """Normalized, non-empty path segments, matching the namespace grammar."""
    return tuple(normalize_name(part) for part in key.split("/") if part)


def _split_key(path: str) -> tuple[tuple[str, ...], str]:
    """Split an object path into its parent folder segments and its final name.

    A key with no name -- an empty or folder-shaped address -- cannot name an
    object to write, so it is a `ValidationError` rendered as `InvalidArgument`.
    """
    segments = _segments(path)
    if not segments:
        raise ValidationError("an object key is required")
    return segments[:-1], segments[-1]


def _dir_segments(prefix: str) -> tuple[str, ...]:
    """The folder-path portion of a prefix.

    A trailing slash means the whole prefix names a folder; otherwise the final
    component is a name filter the listing algorithm applies, not a folder to
    descend into.
    """
    segments = _segments(prefix)
    if prefix.endswith("/"):
        return segments
    return segments[:-1]
