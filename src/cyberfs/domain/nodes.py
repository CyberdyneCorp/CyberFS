"""The filesystem tree.

An adjacency list: every node stores its parent, and paths are derived on read
rather than stored. Renaming a folder therefore rewrites no descendant rows --
see `design.md`, "Adjacency-list tree with derived paths".
"""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from cyberfs.domain.errors import ValidationError

MAX_NAME_LENGTH = 255
#: Rejected outright: path separators, NUL, and the relative-path names.
FORBIDDEN_NAME_CHARS = frozenset({"/", "\\", "\x00"})
RESERVED_NAMES = frozenset({".", ".."})


class NodeKind(StrEnum):
    FOLDER = "folder"
    FILE = "file"


class EncryptionDefault(StrEnum):
    """A folder's instruction to files created inside it."""

    INHERIT = "inherit"
    ON = "on"
    OFF = "off"


def normalize_name(name: str) -> str:
    """Fold a name to its uniqueness key.

    NFC normalization means two spellings of the same character cannot occupy
    the same folder -- otherwise `café` typed two ways would be two siblings
    that render identically.
    """
    return unicodedata.normalize("NFC", name)


def validate_name(name: str) -> str:
    """Return `name` unchanged if it is usable, else raise."""
    if not name or len(name) > MAX_NAME_LENGTH:
        raise ValidationError(f"name must be 1 to {MAX_NAME_LENGTH} characters")
    if FORBIDDEN_NAME_CHARS & set(name):
        raise ValidationError("name may not contain a path separator or NUL")
    if name in RESERVED_NAMES:
        raise ValidationError(f"{name!r} is a reserved name")
    return name


@dataclass(frozen=True, slots=True)
class FileVersion:
    """One immutable revision of a file's content."""

    id: uuid.UUID
    node_id: uuid.UUID
    owner_id: uuid.UUID
    sequence: int
    size_bytes: int
    #: SHA-256 of the *plaintext*, so integrity is verifiable after decryption.
    plaintext_digest: str
    content_type: str
    encrypted: bool
    created_at: datetime
    created_by: str

    @property
    def object_key(self) -> str:
        """Where the bytes live in MinIO.

        Derived entirely from opaque identifiers: a name like `../../etc/passwd`
        can never reach the object store, and rename and move stay pure
        metadata operations.
        """
        return f"{self.owner_id}/{self.node_id}/{self.id}"


@dataclass(slots=True)
class Node:
    """A folder or a file.

    One entity with a discriminator rather than two types: almost every
    operation -- move, rename, share, delete, permission resolution -- treats
    them identically, and the tree invariants are shared.
    """

    id: uuid.UUID
    owner_id: uuid.UUID
    kind: NodeKind
    name: str
    parent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    #: Bumped on every mutation; surfaced as an ETag for `If-Match`.
    revision: int = 0
    deleted_at: datetime | None = None

    # --- files only ---------------------------------------------------
    current_version_id: uuid.UUID | None = None
    size_bytes: int = 0
    content_type: str | None = None
    #: Fixed when the file is created; changing it is an explicit rewrite.
    encrypted: bool = False

    # --- folders only -------------------------------------------------
    encryption_default: EncryptionDefault = EncryptionDefault.INHERIT

    def __post_init__(self) -> None:
        validate_name(self.name)
        if self.is_folder and self.encrypted:
            raise ValidationError("a folder has no content to encrypt")
        if self.is_file and self.encryption_default is not EncryptionDefault.INHERIT:
            raise ValidationError("encryption_default belongs to folders")

    @property
    def normalized_name(self) -> str:
        return normalize_name(self.name)

    @property
    def is_folder(self) -> bool:
        return self.kind is NodeKind.FOLDER

    @property
    def is_file(self) -> bool:
        return self.kind is NodeKind.FILE

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def etag(self) -> str:
        return f'"{self.id.hex}-{self.revision}"'

    def touch(self, now: datetime) -> None:
        self.updated_at = now
        self.revision += 1

    def rename(self, name: str, now: datetime) -> None:
        self.name = validate_name(name)
        self.touch(now)

    def move_to(self, parent_id: uuid.UUID, now: datetime) -> None:
        if self.is_root:
            raise ValidationError("a root folder cannot be moved")
        if parent_id == self.id:
            # The full ancestor walk lives in the repository; this catches the
            # degenerate case without a query.
            raise ValidationError("a node cannot be its own parent")
        self.parent_id = parent_id
        self.touch(now)

    def soft_delete(self, now: datetime) -> None:
        if self.is_root:
            raise ValidationError("a root folder cannot be deleted")
        if self.is_deleted:
            return
        self.deleted_at = now
        self.touch(now)

    def restore(self, now: datetime) -> None:
        self.deleted_at = None
        self.touch(now)


@dataclass(frozen=True, slots=True)
class NodePath:
    """A node with its ancestors, root first.

    Produced by the repository's ancestor walk; paths are rendered from it
    rather than stored on the node.
    """

    node: Node
    ancestors: tuple[Node, ...] = field(default=())

    @property
    def segments(self) -> tuple[str, ...]:
        # The root folder contributes no segment -- a user's tree is presented
        # as `/reports/q3.xlsx`, not `/<root>/reports/q3.xlsx`.
        named = [*self.ancestors, self.node]
        return tuple(n.name for n in named if not n.is_root)

    @property
    def path(self) -> str:
        return "/" + "/".join(self.segments)

    @property
    def depth(self) -> int:
        return len(self.ancestors)
