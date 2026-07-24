"""Tree entities and their invariants."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.domain.errors import ValidationError
from cyberfs.domain.nodes import (
    MAX_NAME_LENGTH,
    EncryptionDefault,
    FileVersion,
    Node,
    NodeKind,
    NodePath,
    normalize_name,
    validate_name,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
OWNER = uuid.uuid4()


#: Distinguishes "no parent given, invent one" from "explicitly a root".
UNSET = object()


def folder(name: str = "reports", parent: object = UNSET, **kw: object) -> Node:
    parent_id = uuid.uuid4() if parent is UNSET else parent
    return Node(
        id=kw.pop("id", uuid.uuid4()),  # type: ignore[arg-type]
        owner_id=OWNER,
        kind=NodeKind.FOLDER,
        name=name,
        parent_id=parent_id,  # type: ignore[arg-type]
        created_at=NOW,
        updated_at=NOW,
        **kw,  # type: ignore[arg-type]
    )


def file_node(name: str = "q3.xlsx", **kw: object) -> Node:
    return Node(
        id=kw.pop("id", uuid.uuid4()),  # type: ignore[arg-type]
        owner_id=OWNER,
        kind=NodeKind.FILE,
        name=name,
        parent_id=kw.pop("parent_id", uuid.uuid4()),  # type: ignore[arg-type]
        created_at=NOW,
        updated_at=NOW,
        **kw,  # type: ignore[arg-type]
    )


# --- names -----------------------------------------------------------------


def test_ordinary_name_is_accepted() -> None:
    assert validate_name("Quarterly Report (final).xlsx")


@pytest.mark.parametrize("name", ["", "x" * (MAX_NAME_LENGTH + 1)])
def test_name_length_is_bounded(name: str) -> None:
    with pytest.raises(ValidationError, match="characters"):
        validate_name(name)


def test_name_at_the_limit_is_accepted() -> None:
    assert validate_name("x" * MAX_NAME_LENGTH)


@pytest.mark.parametrize("name", ["a/b", "a\\b", "a\x00b", "/", "\\"])
def test_path_separators_and_nul_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="separator or NUL"):
        validate_name(name)


@pytest.mark.parametrize("name", [".", ".."])
def test_relative_path_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="reserved"):
        validate_name(name)


def test_dotfiles_are_allowed() -> None:
    assert validate_name(".gitignore")


def test_normalization_folds_equivalent_spellings() -> None:
    """`café` composed and decomposed must not become two siblings."""
    composed = "café"
    decomposed = "café"
    assert composed != decomposed
    assert normalize_name(composed) == normalize_name(decomposed)


def test_node_exposes_its_normalized_name() -> None:
    assert folder(name="café").normalized_name == normalize_name("café")


def test_invalid_name_is_rejected_at_construction() -> None:
    with pytest.raises(ValidationError):
        folder(name="bad/name")


# --- kind invariants -------------------------------------------------------


def test_folder_cannot_be_encrypted() -> None:
    """Folders hold no content; encryption is a property of a file."""
    with pytest.raises(ValidationError, match="no content"):
        folder(encrypted=True)


def test_file_cannot_carry_an_encryption_default() -> None:
    with pytest.raises(ValidationError, match="belongs to folders"):
        file_node(encryption_default=EncryptionDefault.ON)


def test_folder_may_carry_an_encryption_default() -> None:
    assert folder(encryption_default=EncryptionDefault.ON).encryption_default is (
        EncryptionDefault.ON
    )


def test_kind_predicates() -> None:
    assert folder().is_folder and not folder().is_file
    assert file_node().is_file and not file_node().is_folder


def test_root_has_no_parent() -> None:
    assert folder(parent=None).is_root


# --- revisions and etags ---------------------------------------------------


def test_touch_bumps_the_revision() -> None:
    node = folder()
    node.touch(NOW + timedelta(minutes=1))
    assert node.revision == 1
    assert node.updated_at == NOW + timedelta(minutes=1)


def test_etag_changes_with_the_revision() -> None:
    node = folder()
    before = node.etag
    node.touch(NOW)
    assert node.etag != before


def test_etag_is_stable_without_a_mutation() -> None:
    node = folder()
    assert node.etag == node.etag


# --- mutations -------------------------------------------------------------


def test_rename_validates_and_touches() -> None:
    node = folder()
    node.rename("archive", NOW)
    assert node.name == "archive"
    assert node.revision == 1


def test_rename_to_an_invalid_name_is_refused() -> None:
    node = folder()
    with pytest.raises(ValidationError):
        node.rename("a/b", NOW)
    assert node.name == "reports", "a refused rename must not mutate the node"


def test_move_reparents() -> None:
    node = folder()
    target = uuid.uuid4()
    node.move_to(target, NOW)
    assert node.parent_id == target


def test_root_cannot_be_moved() -> None:
    with pytest.raises(ValidationError, match="root"):
        folder(parent=None).move_to(uuid.uuid4(), NOW)


def test_node_cannot_be_its_own_parent() -> None:
    node = folder()
    with pytest.raises(ValidationError, match="own parent"):
        node.move_to(node.id, NOW)


def test_soft_delete_marks_and_touches() -> None:
    node = folder()
    node.soft_delete(NOW)
    assert node.is_deleted
    assert node.deleted_at == NOW


def test_soft_delete_is_idempotent() -> None:
    node = folder()
    node.soft_delete(NOW)
    revision = node.revision
    node.soft_delete(NOW + timedelta(hours=1))
    assert node.deleted_at == NOW
    assert node.revision == revision


def test_root_cannot_be_deleted() -> None:
    with pytest.raises(ValidationError, match="root"):
        folder(parent=None).soft_delete(NOW)


def test_restore_clears_the_deletion() -> None:
    node = folder()
    node.soft_delete(NOW)
    node.restore(NOW)
    assert not node.is_deleted


# --- versions and object keys ---------------------------------------------


def version(**kw: object) -> FileVersion:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "node_id": uuid.uuid4(),
        "owner_id": OWNER,
        "sequence": 1,
        "size_bytes": 1024,
        "plaintext_digest": "abc123",
        "content_type": "application/octet-stream",
        "encrypted": False,
        "created_at": NOW,
        "created_by": "user-1",
    }
    return FileVersion(**{**base, **kw})  # type: ignore[arg-type]


def test_object_key_is_built_from_identifiers() -> None:
    v = version()
    assert v.object_key == f"{v.owner_id}/{v.node_id}/{v.id}"


def test_object_key_contains_no_user_supplied_text() -> None:
    """A name like `../../etc/passwd` must never reach the object store."""
    key = version().object_key
    assert ".." not in key
    assert key.count("/") == 2


def test_versions_of_one_node_get_distinct_keys() -> None:
    node_id = uuid.uuid4()
    first = version(node_id=node_id)
    second = version(node_id=node_id)
    assert first.object_key != second.object_key


# --- paths -----------------------------------------------------------------


def test_path_is_derived_from_ancestors() -> None:
    root = folder(name="root", parent=None)
    reports = folder(name="reports")
    leaf = file_node(name="q3.xlsx")
    assert NodePath(leaf, (root, reports)).path == "/reports/q3.xlsx"


def test_root_contributes_no_segment() -> None:
    root = folder(name="root", parent=None)
    assert NodePath(file_node(name="a.txt"), (root,)).path == "/a.txt"


def test_path_of_a_node_at_the_root() -> None:
    assert NodePath(folder(name="reports"), ()).path == "/reports"


def test_depth_counts_ancestors() -> None:
    root = folder(name="root", parent=None)
    mid = folder(name="mid")
    assert NodePath(file_node(), (root, mid)).depth == 2


def test_renaming_a_folder_changes_descendant_paths_without_touching_them() -> None:
    """The point of deriving paths: a rename is a single-row write."""
    root = folder(name="root", parent=None)
    reports = folder(name="reports")
    leaf = file_node(name="q3.xlsx")
    leaf_revision = leaf.revision

    reports.rename("archive", NOW)

    assert NodePath(leaf, (root, reports)).path == "/archive/q3.xlsx"
    assert leaf.revision == leaf_revision, "the descendant was not rewritten"
