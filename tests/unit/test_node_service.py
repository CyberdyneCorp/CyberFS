"""Tree use cases -- `file-storage/spec.md`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.nodes import NodeService, NodeView
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import (
    CrossOwnerMoveError,
    NameTakenError,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
    ValidationError,
    WouldCreateCycleError,
)
from cyberfs.domain.nodes import EncryptionDefault, Node, NodeKind
from cyberfs.domain.sharing import Grant, Role
from cyberfs.domain.users import User

from .fakes import FakeKeyProvider, FakeUnitOfWork

World = tuple[FakeUnitOfWork, User, NodeService]

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3


def service(**kw: object) -> NodeService:
    return NodeService(
        max_tree_depth=kw.pop("max_tree_depth", 64),  # type: ignore[arg-type]
        page_size_max=kw.pop("page_size_max", 1000),  # type: ignore[arg-type]
    )


async def provision(uow: FakeUnitOfWork, subject: str = "alice") -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


async def add_file(
    uow: FakeUnitOfWork, user: User, name: str, parent: uuid.UUID, size: int = 0
) -> Node:
    node = Node(
        id=uuid.uuid4(),
        owner_id=user.id,
        kind=NodeKind.FILE,
        name=name,
        parent_id=parent,
        created_at=NOW,
        updated_at=NOW,
        size_bytes=size,
    )
    await uow.nodes.add(node)
    return node


async def grant(uow: FakeUnitOfWork, node_id: uuid.UUID, subject: str, role: Role) -> None:
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=node_id,
            subject=subject,
            role=role,
            granted_by="alice",
            created_at=NOW,
            updated_at=NOW,
        )
    )


@pytest.fixture
async def world() -> World:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    return uow, user, service()


# --- create ----------------------------------------------------------------


async def test_folder_is_created_in_the_root(world: World) -> None:
    uow, user, svc = world
    view = await svc.create_folder(uow, user, user.root_folder_id, "reports", now=NOW)

    assert view.node.name == "reports"
    assert view.node.parent_id == user.root_folder_id
    assert view.role is Role.OWNER


async def test_created_folder_reports_its_path(
    world: World,
) -> None:
    uow, user, svc = world
    outer = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "b", now=NOW)
    assert inner.path == "/a/b"


async def test_duplicate_sibling_name_is_refused(
    world: World,
) -> None:
    uow, user, svc = world
    await svc.create_folder(uow, user, user.root_folder_id, "reports", now=NOW)
    with pytest.raises(NameTakenError):
        await svc.create_folder(uow, user, user.root_folder_id, "reports", now=NOW)


async def test_same_name_in_different_folders_is_allowed(
    world: World,
) -> None:
    uow, user, svc = world
    a = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    b = await svc.create_folder(uow, user, user.root_folder_id, "b", now=NOW)
    await svc.create_folder(uow, user, a.node.id, "shared", now=NOW)
    await svc.create_folder(uow, user, b.node.id, "shared", now=NOW)


@pytest.mark.parametrize("name", ["a/b", "a\\b", ".", "..", "", "x" * 256])
async def test_invalid_names_are_refused(world: World, name: str) -> None:
    uow, user, svc = world
    with pytest.raises(ValidationError):
        await svc.create_folder(uow, user, user.root_folder_id, name, now=NOW)


async def test_shared_is_reserved_at_the_root(world: World) -> None:
    """`s3-compatibility/spec.md`: a real folder cannot shadow the shared view."""
    uow, user, svc = world
    with pytest.raises(ValidationError):
        await svc.create_folder(uow, user, user.root_folder_id, "shared", now=NOW)


async def test_shared_is_only_reserved_at_the_root(world: World) -> None:
    """The reservation is root-scoped: `shared` stays legal deeper in the tree."""
    uow, user, svc = world
    parent = await svc.create_folder(uow, user, user.root_folder_id, "docs", now=NOW)
    child = await svc.create_folder(uow, user, parent.node.id, "shared", now=NOW)
    assert child.node.name == "shared"


async def test_encryption_default_is_recorded(
    world: World,
) -> None:
    uow, user, svc = world
    view = await svc.create_folder(
        uow, user, user.root_folder_id, "secret", encryption_default=EncryptionDefault.ON, now=NOW
    )
    assert view.node.encryption_default is EncryptionDefault.ON


async def test_creating_inside_a_file_is_refused(
    world: World,
) -> None:
    uow, user, svc = world
    node = await add_file(uow, user, "notes.txt", user.root_folder_id)
    with pytest.raises(ValidationError, match="folder"):
        await svc.create_folder(uow, user, node.id, "child", now=NOW)


async def test_depth_limit_is_enforced(world: World) -> None:
    uow, user, _ = world
    svc = service(max_tree_depth=3)
    parent = user.root_folder_id
    for i in range(2):
        parent = (await svc.create_folder(uow, user, parent, f"level-{i}", now=NOW)).node.id
    with pytest.raises(ValidationError, match="depth"):
        await svc.create_folder(uow, user, parent, "too-deep", now=NOW)


# --- read ------------------------------------------------------------------


async def test_owner_reads_their_node(world: World) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "reports", now=NOW)
    view = await svc.get(uow, user, created.node.id)
    assert view.node.id == created.node.id
    assert view.role is Role.OWNER


async def test_unknown_node_is_not_found(world: World) -> None:
    uow, user, svc = world
    with pytest.raises(NotFoundError):
        await svc.get(uow, user, uuid.uuid4())


async def test_another_users_node_is_not_found_rather_than_forbidden() -> None:
    """A 403 would confirm the node exists; `file-storage/spec.md` says 404."""
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()
    hers = await svc.create_folder(uow, alice, alice.root_folder_id, "private", now=NOW)

    with pytest.raises(NotFoundError):
        await svc.get(uow, bob, hers.node.id)


async def test_trashed_node_is_not_found(world: World) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "gone", now=NOW)
    await svc.delete(uow, user, created.node.id, now=NOW)
    with pytest.raises(NotFoundError):
        await svc.get(uow, user, created.node.id)


# --- listing ---------------------------------------------------------------


async def test_children_are_listed(world: World) -> None:
    uow, user, svc = world
    await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    await add_file(uow, user, "b.txt", user.root_folder_id)

    page = await svc.list_children(uow, user, user.root_folder_id, limit=10)
    assert {n.name for n in page.items} == {"a", "b.txt"}


async def test_folders_are_listed_before_files(
    world: World,
) -> None:
    uow, user, svc = world
    await add_file(uow, user, "a-file.txt", user.root_folder_id)
    await svc.create_folder(uow, user, user.root_folder_id, "z-folder", now=NOW)

    page = await svc.list_children(uow, user, user.root_folder_id, limit=10)
    assert [n.name for n in page.items] == ["z-folder", "a-file.txt"]


async def test_listing_a_file_is_refused(world: World) -> None:
    uow, user, svc = world
    node = await add_file(uow, user, "notes.txt", user.root_folder_id)
    with pytest.raises(ValidationError, match="children"):
        await svc.list_children(uow, user, node.id, limit=10)


async def test_page_size_is_capped(world: World) -> None:
    uow, user, _ = world
    svc = service(page_size_max=2)
    for i in range(5):
        await add_file(uow, user, f"f{i}.txt", user.root_folder_id)

    page = await svc.list_children(uow, user, user.root_folder_id, limit=1000)
    assert len(page.items) == 2


# --- rename ----------------------------------------------------------------


async def test_rename_changes_the_name(world: World) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "old", now=NOW)
    view = await svc.rename(uow, user, created.node.id, "new", now=LATER)
    assert view.node.name == "new"


async def test_rename_to_a_taken_name_is_refused(
    world: World,
) -> None:
    uow, user, svc = world
    await svc.create_folder(uow, user, user.root_folder_id, "taken", now=NOW)
    other = await svc.create_folder(uow, user, user.root_folder_id, "free", now=NOW)
    with pytest.raises(NameTakenError):
        await svc.rename(uow, user, other.node.id, "taken", now=LATER)


async def test_renaming_a_node_to_its_own_name_is_allowed(
    world: World,
) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "same", now=NOW)
    await svc.rename(uow, user, created.node.id, "same", now=LATER)


async def test_root_cannot_be_renamed(world: World) -> None:
    uow, user, svc = world
    with pytest.raises(ValidationError, match="root"):
        await svc.rename(uow, user, user.root_folder_id, "mine", now=LATER)


async def test_renaming_a_folder_leaves_descendants_untouched(
    world: World,
) -> None:
    """Paths are derived, so a rename is a single-row write."""
    uow, user, svc = world
    folder = await svc.create_folder(uow, user, user.root_folder_id, "reports", now=NOW)
    leaf = await add_file(uow, user, "q3.xlsx", folder.node.id)
    before = leaf.revision

    await svc.rename(uow, user, folder.node.id, "archive", now=LATER)

    view = await svc.get(uow, user, leaf.id)
    assert view.path == "/archive/q3.xlsx"
    assert leaf.revision == before


# --- optimistic concurrency ------------------------------------------------


async def test_matching_if_match_is_accepted(
    world: World,
) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    await svc.rename(uow, user, created.node.id, "b", if_match=created.etag, now=LATER)


async def test_stale_if_match_is_rejected(
    world: World,
) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    stale = created.etag
    await svc.rename(uow, user, created.node.id, "b", now=LATER)

    with pytest.raises(PreconditionFailedError):
        await svc.rename(uow, user, created.node.id, "c", if_match=stale, now=LATER)


async def test_wildcard_if_match_always_matches(
    world: World,
) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    await svc.rename(uow, user, created.node.id, "b", if_match="*", now=LATER)


async def test_absent_if_match_skips_the_check(
    world: World,
) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    await svc.rename(uow, user, created.node.id, "b", if_match=None, now=LATER)


async def test_a_refused_precondition_does_not_mutate(
    world: World,
) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    with pytest.raises(PreconditionFailedError):
        await svc.rename(uow, user, created.node.id, "c", if_match='"bogus-0"', now=LATER)

    assert (await svc.get(uow, user, created.node.id)).node.name == "a"


# --- move ------------------------------------------------------------------


async def test_move_reparents(world: World) -> None:
    uow, user, svc = world
    target = await svc.create_folder(uow, user, user.root_folder_id, "target", now=NOW)
    node = await add_file(uow, user, "notes.txt", user.root_folder_id)

    view = await svc.move(uow, user, node.id, target.node.id, now=LATER)
    assert view.node.parent_id == target.node.id
    assert view.path == "/target/notes.txt"


async def test_moving_a_folder_into_itself_is_refused(
    world: World,
) -> None:
    uow, user, svc = world
    folder = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    with pytest.raises((WouldCreateCycleError, ValidationError)):
        await svc.move(uow, user, folder.node.id, folder.node.id, now=LATER)


async def test_moving_a_folder_into_its_own_descendant_is_refused(
    world: World,
) -> None:
    uow, user, svc = world
    outer = await svc.create_folder(uow, user, user.root_folder_id, "outer", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "inner", now=NOW)

    with pytest.raises(WouldCreateCycleError):
        await svc.move(uow, user, outer.node.id, inner.node.id, now=LATER)


async def test_root_cannot_be_moved(world: World) -> None:
    uow, user, svc = world
    target = await svc.create_folder(uow, user, user.root_folder_id, "t", now=NOW)
    with pytest.raises(ValidationError, match="root"):
        await svc.move(uow, user, user.root_folder_id, target.node.id, now=LATER)


async def test_move_across_owners_is_refused() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()

    hers = await svc.create_folder(uow, alice, alice.root_folder_id, "hers", now=NOW)
    # Bob is an editor on Alice's folder but his root is still his own.
    await grant(uow, hers.node.id, "bob", Role.EDITOR)
    node = await add_file(uow, alice, "notes.txt", hers.node.id)

    with pytest.raises(CrossOwnerMoveError):
        await svc.move(uow, bob, node.id, bob.root_folder_id, now=LATER)


async def test_move_into_a_taken_name_is_refused(
    world: World,
) -> None:
    uow, user, svc = world
    target = await svc.create_folder(uow, user, user.root_folder_id, "target", now=NOW)
    await add_file(uow, user, "notes.txt", target.node.id)
    moving = await add_file(uow, user, "notes.txt", user.root_folder_id)

    with pytest.raises(NameTakenError):
        await svc.move(uow, user, moving.id, target.node.id, now=LATER)


async def test_move_takes_a_lock_on_the_destination(
    world: World,
) -> None:
    """Serializes concurrent moves that could jointly create a cycle."""
    uow, user, svc = world
    target = await svc.create_folder(uow, user, user.root_folder_id, "t", now=NOW)
    node = await add_file(uow, user, "n.txt", user.root_folder_id)

    locked: list[uuid.UUID] = []

    async def record(node_id: uuid.UUID) -> None:
        locked.append(node_id)

    uow.lock_subtree = record  # type: ignore[method-assign]

    await svc.move(uow, user, node.id, target.node.id, now=LATER)
    assert target.node.id in locked


# --- delete and restore ----------------------------------------------------


async def test_delete_covers_the_subtree(
    world: World,
) -> None:
    uow, user, svc = world
    outer = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "b", now=NOW)
    await add_file(uow, user, "leaf.txt", inner.node.id)

    assert await svc.delete(uow, user, outer.node.id, now=LATER) == 3


async def test_root_cannot_be_deleted(world: World) -> None:
    uow, user, svc = world
    with pytest.raises(ValidationError, match="root"):
        await svc.delete(uow, user, user.root_folder_id, now=LATER)


async def test_editor_cannot_delete() -> None:
    """`sharing/spec.md`: deleting requires owner."""
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()
    folder = await svc.create_folder(uow, alice, alice.root_folder_id, "team", now=NOW)
    await grant(uow, folder.node.id, "bob", Role.EDITOR)

    with pytest.raises(PermissionDeniedError):
        await svc.delete(uow, bob, folder.node.id, now=LATER)


async def test_restore_returns_the_node(
    world: World,
) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "back", now=NOW)
    await svc.delete(uow, user, created.node.id, now=LATER)

    view = await svc.restore(uow, user, created.node.id, now=LATER)
    assert not view.node.is_deleted
    assert view.node.parent_id == user.root_folder_id


async def test_restore_of_a_live_node_is_not_found(
    world: World,
) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "live", now=NOW)
    with pytest.raises(NotFoundError):
        await svc.restore(uow, user, created.node.id, now=LATER)


async def test_restore_falls_back_to_the_root_when_the_parent_is_gone(
    world: World,
) -> None:
    uow, user, svc = world
    parent = await svc.create_folder(uow, user, user.root_folder_id, "parent", now=NOW)
    child = await svc.create_folder(uow, user, parent.node.id, "child", now=NOW)
    await svc.delete(uow, user, parent.node.id, now=LATER)
    await uow.nodes.delete_permanently(parent.node.id)

    view = await svc.restore(uow, user, child.node.id, now=LATER)
    assert view.node.parent_id == user.root_folder_id


# --- copy ------------------------------------------------------------------


async def test_copy_duplicates_a_folder_subtree(
    world: World,
) -> None:
    uow, user, svc = world
    source = await svc.create_folder(uow, user, user.root_folder_id, "source", now=NOW)
    await svc.create_folder(uow, user, source.node.id, "nested", now=NOW)
    target = await svc.create_folder(uow, user, user.root_folder_id, "target", now=NOW)

    view = await svc.copy(uow, user, source.node.id, target.node.id, now=LATER)

    assert view.node.parent_id == target.node.id
    children = await svc.list_children(uow, user, view.node.id, limit=10)
    assert [n.name for n in children.items] == ["nested"]


async def test_copy_can_be_renamed(world: World) -> None:
    uow, user, svc = world
    source = await svc.create_folder(uow, user, user.root_folder_id, "source", now=NOW)
    view = await svc.copy(uow, user, source.node.id, user.root_folder_id, name="copy", now=LATER)
    assert view.node.name == "copy"


async def test_copying_into_the_same_folder_needs_a_new_name(
    world: World,
) -> None:
    uow, user, svc = world
    source = await svc.create_folder(uow, user, user.root_folder_id, "source", now=NOW)
    with pytest.raises(NameTakenError):
        await svc.copy(uow, user, source.node.id, user.root_folder_id, now=LATER)


async def test_copying_a_folder_into_itself_is_refused(
    world: World,
) -> None:
    uow, user, svc = world
    source = await svc.create_folder(uow, user, user.root_folder_id, "source", now=NOW)
    inner = await svc.create_folder(uow, user, source.node.id, "inner", now=NOW)
    with pytest.raises(WouldCreateCycleError):
        await svc.copy(uow, user, source.node.id, inner.node.id, now=LATER)


async def test_a_copy_belongs_to_the_copier_and_carries_no_grants() -> None:
    """`sharing/spec.md`: a copy is visible only to its new owner."""
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()

    hers = await svc.create_folder(uow, alice, alice.root_folder_id, "hers", now=NOW)
    await grant(uow, hers.node.id, "bob", Role.VIEWER)

    view = await svc.copy(uow, bob, hers.node.id, bob.root_folder_id, now=LATER)

    assert view.node.owner_id == bob.id
    assert await uow.grants.list_for_node(view.node.id) == ()


async def test_copy_requires_only_viewer_on_the_source() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()
    hers = await svc.create_folder(uow, alice, alice.root_folder_id, "hers", now=NOW)
    await grant(uow, hers.node.id, "bob", Role.VIEWER)

    await svc.copy(uow, bob, hers.node.id, bob.root_folder_id, now=LATER)


async def test_copy_without_access_to_the_source_is_not_found() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()
    hers = await svc.create_folder(uow, alice, alice.root_folder_id, "private", now=NOW)

    with pytest.raises(NotFoundError):
        await svc.copy(uow, bob, hers.node.id, bob.root_folder_id, now=LATER)


# --- inherited permission --------------------------------------------------


async def test_a_folder_grant_reaches_descendants() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()

    folder = await svc.create_folder(uow, alice, alice.root_folder_id, "team", now=NOW)
    leaf = await add_file(uow, alice, "inside.txt", folder.node.id)
    await grant(uow, folder.node.id, "bob", Role.VIEWER)

    view = await svc.get(uow, bob, leaf.id)
    assert view.role is Role.VIEWER


async def test_the_highest_role_wins() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()

    folder = await svc.create_folder(uow, alice, alice.root_folder_id, "team", now=NOW)
    leaf = await add_file(uow, alice, "inside.txt", folder.node.id)
    await grant(uow, folder.node.id, "bob", Role.VIEWER)
    await grant(uow, leaf.id, "bob", Role.EDITOR)

    assert (await svc.get(uow, bob, leaf.id)).role is Role.EDITOR


async def test_an_ancestor_grant_is_not_narrowed_by_a_lower_direct_grant() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()

    folder = await svc.create_folder(uow, alice, alice.root_folder_id, "team", now=NOW)
    leaf = await add_file(uow, alice, "inside.txt", folder.node.id)
    await grant(uow, folder.node.id, "bob", Role.EDITOR)
    await grant(uow, leaf.id, "bob", Role.VIEWER)

    assert (await svc.get(uow, bob, leaf.id)).role is Role.EDITOR


async def test_a_viewer_cannot_write() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = service()
    folder = await svc.create_folder(uow, alice, alice.root_folder_id, "team", now=NOW)
    await grant(uow, folder.node.id, "bob", Role.VIEWER)

    with pytest.raises(PermissionDeniedError):
        await svc.create_folder(uow, bob, folder.node.id, "mine", now=LATER)


# --- search ----------------------------------------------------------------


async def test_search_matches_names(world: World) -> None:
    uow, user, svc = world
    await add_file(uow, user, "quarterly-report.xlsx", user.root_folder_id)
    await add_file(uow, user, "notes.txt", user.root_folder_id)

    results = await svc.search(uow, user, "report", limit=10)
    assert [n.name for n in results] == ["quarterly-report.xlsx"]


async def test_search_requires_a_term(world: World) -> None:
    uow, user, svc = world
    with pytest.raises(ValidationError):
        await svc.search(uow, user, "   ", limit=10)


async def test_search_excludes_trashed_nodes(
    world: World,
) -> None:
    uow, user, svc = world
    node = await add_file(uow, user, "report.txt", user.root_folder_id)
    await svc.delete(uow, user, node.id, now=LATER)

    assert await svc.search(uow, user, "report", limit=10) == ()


# --- view ------------------------------------------------------------------


async def test_view_exposes_the_etag(world: World) -> None:
    uow, user, svc = world
    created = await svc.create_folder(uow, user, user.root_folder_id, "a", now=NOW)
    assert isinstance(created, NodeView)
    assert created.etag == created.node.etag


# --- copy: content and quota ----------------------------------------------


class StubDuplicator:
    """Stands in for the object store until the storage capability lands."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def duplicate(self, uow: object, source: Node, target: Node, now: datetime) -> int:
        self.calls.append((source.id, target.id))
        return self.size


async def test_copying_a_file_duplicates_its_content(world: World) -> None:
    uow, user, svc = world
    source = await add_file(uow, user, "data.bin", user.root_folder_id, size=500)
    target = await svc.create_folder(uow, user, user.root_folder_id, "target", now=NOW)
    duplicator = StubDuplicator(500)

    view = await svc.copy(uow, user, source.id, target.node.id, content=duplicator, now=LATER)

    assert duplicator.calls == [(source.id, view.node.id)]
    assert view.node.size_bytes == 500


async def test_a_copy_is_charged_to_the_copier(world: World) -> None:
    """`file-storage/spec.md`: a copy is new storage for whoever made it."""
    uow, user, svc = world
    source = await add_file(uow, user, "data.bin", user.root_folder_id, size=500)
    target = await svc.create_folder(uow, user, user.root_folder_id, "target", now=NOW)

    await svc.copy(uow, user, source.id, target.node.id, content=StubDuplicator(500), now=LATER)

    usage = await uow.quotas.get(user.id)
    assert usage is not None
    assert usage.live_bytes == 500


async def test_a_copy_exceeding_the_quota_is_refused(world: World) -> None:
    from cyberfs.domain.errors import QuotaExceededError

    uow, user, svc = world
    user.quota_bytes = 100
    source = await add_file(uow, user, "big.bin", user.root_folder_id, size=500)
    target = await svc.create_folder(uow, user, user.root_folder_id, "target", now=NOW)

    with pytest.raises(QuotaExceededError):
        await svc.copy(uow, user, source.id, target.node.id, content=StubDuplicator(500), now=LATER)


async def test_a_folder_copy_charges_nothing_by_itself(world: World) -> None:
    uow, user, svc = world
    source = await svc.create_folder(uow, user, user.root_folder_id, "empty", now=NOW)
    target = await svc.create_folder(uow, user, user.root_folder_id, "target", now=NOW)

    await svc.copy(uow, user, source.node.id, target.node.id, content=StubDuplicator(0), now=LATER)

    usage = await uow.quotas.get(user.id)
    assert usage is not None
    assert usage.live_bytes == 0


async def test_copy_without_a_duplicator_copies_metadata_only(world: World) -> None:
    """Until the object store is wired, a file copy carries no bytes."""
    uow, user, svc = world
    source = await add_file(uow, user, "data.bin", user.root_folder_id, size=500)
    target = await svc.create_folder(uow, user, user.root_folder_id, "target", now=NOW)

    view = await svc.copy(uow, user, source.id, target.node.id, now=LATER)
    assert view.node.size_bytes == 0


async def test_nested_files_are_copied_with_their_content(world: World) -> None:
    uow, user, svc = world
    source = await svc.create_folder(uow, user, user.root_folder_id, "source", now=NOW)
    await add_file(uow, user, "inner.bin", source.node.id, size=200)
    target = await svc.create_folder(uow, user, user.root_folder_id, "target", now=NOW)
    duplicator = StubDuplicator(200)

    await svc.copy(uow, user, source.node.id, target.node.id, content=duplicator, now=LATER)

    assert len(duplicator.calls) == 1
