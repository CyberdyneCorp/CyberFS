"""Request and response models.

The wire contract. Domain entities are never serialized directly, so a field
added to an entity cannot leak into an API response by accident.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from cyberfs.application.nodes import NodeView
from cyberfs.domain.nodes import EncryptionDefault, FileVersion, Node, NodeKind
from cyberfs.domain.ports.repositories import Page

MAX_NAME_LENGTH = 255


class CreateFolderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    encryption_default: EncryptionDefault = EncryptionDefault.INHERIT


class RenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)


class MoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: uuid.UUID


class CopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: uuid.UUID
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)


class NodeSummary(BaseModel):
    """A node in a listing. No content, and no key material anywhere."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: NodeKind
    name: str
    size_bytes: int
    content_type: str | None
    encrypted: bool
    encryption_default: EncryptionDefault | None
    created_at: datetime
    updated_at: datetime
    etag: str

    @classmethod
    def of(cls, node: Node) -> NodeSummary:
        return cls(
            id=node.id,
            parent_id=node.parent_id,
            kind=node.kind,
            name=node.name,
            size_bytes=node.size_bytes,
            content_type=node.content_type,
            encrypted=node.encrypted,
            # Meaningless on a file; omitted rather than reported as "inherit".
            encryption_default=node.encryption_default if node.is_folder else None,
            created_at=node.created_at,
            updated_at=node.updated_at,
            etag=node.etag,
        )


class NodeDetail(NodeSummary):
    """A single node, with the caller's own permission and its derived path."""

    path: str
    #: The caller's effective role: `viewer`, `editor`, or `owner`.
    role: str

    @classmethod
    def of_view(cls, view: NodeView) -> NodeDetail:
        summary = NodeSummary.of(view.node)
        return cls(**summary.model_dump(), path=view.path, role=view.role.slug)


class NodePage(BaseModel):
    items: list[NodeSummary]
    next_cursor: str | None = None

    @classmethod
    def of(cls, page: Page[Node]) -> NodePage:
        return cls(
            items=[NodeSummary.of(node) for node in page.items],
            next_cursor=page.next_cursor,
        )


class SearchResults(BaseModel):
    items: list[NodeSummary]

    @classmethod
    def of(cls, nodes: tuple[Node, ...]) -> SearchResults:
        return cls(items=[NodeSummary.of(node) for node in nodes])


class DeleteResult(BaseModel):
    #: How many nodes the recursive soft delete covered.
    deleted: int


class VersionSummary(BaseModel):
    """One retained revision. Carries no key material and no object key."""

    id: uuid.UUID
    sequence: int
    size_bytes: int
    content_type: str
    encrypted: bool
    created_at: datetime
    created_by: str
    is_current: bool = False

    @classmethod
    def of(cls, version: FileVersion, *, current: bool = False) -> VersionSummary:
        return cls(
            id=version.id,
            sequence=version.sequence,
            size_bytes=version.size_bytes,
            content_type=version.content_type,
            encrypted=version.encrypted,
            created_at=version.created_at,
            created_by=version.created_by,
            is_current=current,
        )


class VersionList(BaseModel):
    items: list[VersionSummary]

    @classmethod
    def of(cls, versions: tuple[FileVersion, ...]) -> VersionList:
        newest = versions[0].id if versions else None
        return cls(items=[VersionSummary.of(v, current=v.id == newest) for v in versions])
