"""Repositories against a real Postgres.

What only a real database can tell us: that the partial unique index behaves
the way the trash design assumes, that the recursive CTEs walk the tree
correctly and stay bounded, and that transactions roll back cleanly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from cyberfs.adapters.outbound.db.unit_of_work import SqlUnitOfWork
from cyberfs.domain.errors import NameTakenError
from cyberfs.domain.keys import UserKey
from cyberfs.domain.nodes import EncryptionDefault, Node, NodeKind
from cyberfs.domain.users import QuotaUsage, User

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
GB = 1024**3
GUARD = 512


async def make_user(uow: SqlUnitOfWork, subject: str = "user-1") -> User:
    user_id = uuid.uuid4()
    root = Node(
        id=uuid.uuid4(),
        owner_id=user_id,
        kind=NodeKind.FOLDER,
        name="root",
        parent_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    user = User(
        id=user_id,
        subject=subject,
        root_folder_id=root.id,
        quota_bytes=10 * GB,
        created_at=NOW,
        updated_at=NOW,
    )
    await uow.users.add(user)
    # The node references the user; without an ORM relationship the insert
    # order is not derived automatically.
    await uow.flush()
    await uow.nodes.add(root)
    await uow.flush()
    return user


async def add_folder(uow: SqlUnitOfWork, user: User, name: str, parent: uuid.UUID) -> Node:
    node = Node(
        id=uuid.uuid4(),
        owner_id=user.id,
        kind=NodeKind.FOLDER,
        name=name,
        parent_id=parent,
        created_at=NOW,
        updated_at=NOW,
    )
    await uow.nodes.add(node)
    await uow.flush()
    return node


async def add_file(
    uow: SqlUnitOfWork, user: User, name: str, parent: uuid.UUID, size: int = 0
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
    await uow.flush()
    return node


# --- users -----------------------------------------------------------------


async def test_user_round_trips(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    fetched = await uow.users.get_by_subject("user-1")
    assert fetched is not None
    assert fetched.id == user.id
    assert fetched.quota_bytes == 10 * GB


async def test_unknown_subject_is_none(uow: SqlUnitOfWork) -> None:
    assert await uow.users.get_by_subject("nobody") is None


async def test_subject_is_unique(uow: SqlUnitOfWork) -> None:
    await make_user(uow, "dupe")
    with pytest.raises(IntegrityError):
        await make_user(uow, "dupe")


async def test_org_claims_survive_a_round_trip(uow: SqlUnitOfWork) -> None:
    from cyberfs.domain.auth.principal import Org

    user = await make_user(uow)
    user.refresh_from_claims(
        is_admin=True,
        org=Org(id="org-a", short_name="alpha", github_login="alpha-inc"),
        orgs=(Org(id="org-a", short_name="alpha"),),
        now=NOW,
    )
    await uow.users.update(user)
    await uow.commit()

    fetched = await uow.users.get(user.id)
    assert fetched is not None
    assert fetched.is_admin
    assert fetched.org is not None
    assert fetched.org.github_login == "alpha-inc"
    assert len(fetched.orgs) == 1


# --- sibling names ---------------------------------------------------------


async def test_duplicate_live_sibling_name_is_refused(uow: SqlUnitOfWork) -> None:
    """The constraint violation surfaces as a domain error, not a driver one."""
    user = await make_user(uow)
    await add_folder(uow, user, "reports", user.root_folder_id)
    with pytest.raises(NameTakenError):
        await add_folder(uow, user, "reports", user.root_folder_id)


async def test_same_name_in_different_folders_is_fine(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    b = await add_folder(uow, user, "b", user.root_folder_id)
    await add_file(uow, user, "notes.txt", a.id)
    await add_file(uow, user, "notes.txt", b.id)


async def test_a_name_is_reusable_once_its_holder_is_trashed(uow: SqlUnitOfWork) -> None:
    """The whole point of the partial unique index."""
    user = await make_user(uow)
    original = await add_file(uow, user, "notes.txt", user.root_folder_id)

    original.soft_delete(NOW)
    await uow.nodes.update(original)
    await uow.flush()

    replacement = await add_file(uow, user, "notes.txt", user.root_folder_id)
    assert replacement.id != original.id


async def test_lookup_by_name_ignores_trashed_rows(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    node = await add_file(uow, user, "notes.txt", user.root_folder_id)
    node.soft_delete(NOW)
    await uow.nodes.update(node)
    await uow.flush()

    assert await uow.nodes.get_child_by_name(user.root_folder_id, "notes.txt") is None


async def test_unicode_spellings_collide(uow: SqlUnitOfWork) -> None:
    """`café` composed and decomposed must not become two siblings."""
    user = await make_user(uow)
    await add_folder(uow, user, "café", user.root_folder_id)
    with pytest.raises(NameTakenError):
        await add_folder(uow, user, "café", user.root_folder_id)


# --- listings --------------------------------------------------------------


async def test_children_are_listed_folders_first(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    await add_file(uow, user, "a-file.txt", user.root_folder_id)
    await add_folder(uow, user, "z-folder", user.root_folder_id)

    page = await uow.nodes.list_children(user.root_folder_id, limit=10)
    assert [n.name for n in page.items] == ["z-folder", "a-file.txt"]


async def test_listing_excludes_trashed_by_default(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    kept = await add_file(uow, user, "kept.txt", user.root_folder_id)
    gone = await add_file(uow, user, "gone.txt", user.root_folder_id)
    gone.soft_delete(NOW)
    await uow.nodes.update(gone)
    await uow.flush()

    page = await uow.nodes.list_children(user.root_folder_id, limit=10)
    assert [n.id for n in page.items] == [kept.id]


async def test_listing_pages_with_a_cursor(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    for i in range(5):
        await add_file(uow, user, f"file-{i}.txt", user.root_folder_id)

    first = await uow.nodes.list_children(user.root_folder_id, limit=2)
    assert len(first.items) == 2
    assert first.has_more

    second = await uow.nodes.list_children(user.root_folder_id, limit=2, cursor=first.next_cursor)
    assert len(second.items) == 2
    assert {n.id for n in first.items}.isdisjoint({n.id for n in second.items})


async def test_last_page_has_no_cursor(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    await add_file(uow, user, "only.txt", user.root_folder_id)
    page = await uow.nodes.list_children(user.root_folder_id, limit=10)
    assert not page.has_more


# --- recursive CTEs --------------------------------------------------------


async def test_ancestors_are_returned_root_first(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    b = await add_folder(uow, user, "b", a.id)
    leaf = await add_file(uow, user, "leaf.txt", b.id)

    chain = await uow.nodes.ancestors(leaf.id, max_depth=GUARD)
    assert [n.name for n in chain] == ["root", "a", "b"]


async def test_ancestors_of_a_root_is_empty(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    assert await uow.nodes.ancestors(user.root_folder_id, max_depth=GUARD) == ()


async def test_ancestor_walk_is_depth_bounded(uow: SqlUnitOfWork) -> None:
    """A bound means a corrupted parent link cannot hang a request."""
    user = await make_user(uow)
    parent = user.root_folder_id
    for i in range(6):
        parent = (await add_folder(uow, user, f"level-{i}", parent)).id
    leaf = await add_file(uow, user, "leaf.txt", parent)

    assert len(await uow.nodes.ancestors(leaf.id, max_depth=3)) <= 4


async def test_descendants_span_the_subtree(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    b = await add_folder(uow, user, "b", a.id)
    await add_file(uow, user, "deep.txt", b.id)
    await add_file(uow, user, "shallow.txt", a.id)

    names = {n.name for n in await uow.nodes.descendants(a.id, max_depth=GUARD)}
    assert names == {"b", "deep.txt", "shallow.txt"}


async def test_descendants_exclude_trashed_by_default(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    gone = await add_file(uow, user, "gone.txt", a.id)
    gone.soft_delete(NOW)
    await uow.nodes.update(gone)
    await uow.flush()

    assert await uow.nodes.descendants(a.id, max_depth=GUARD) == ()


async def test_descendants_of_a_leaf_is_empty(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    leaf = await add_file(uow, user, "leaf.txt", user.root_folder_id)
    assert await uow.nodes.descendants(leaf.id, max_depth=GUARD) == ()


async def test_a_sibling_subtree_is_not_included(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    b = await add_folder(uow, user, "b", user.root_folder_id)
    await add_file(uow, user, "in-b.txt", b.id)

    assert await uow.nodes.descendants(a.id, max_depth=GUARD) == ()


# --- cycle detection -------------------------------------------------------


async def test_ancestor_relationship_is_detected(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    b = await add_folder(uow, user, "b", a.id)

    assert await uow.nodes.is_ancestor_of(a.id, b.id)
    assert not await uow.nodes.is_ancestor_of(b.id, a.id)


async def test_a_node_is_its_own_ancestor_for_cycle_purposes(uow: SqlUnitOfWork) -> None:
    """Moving a folder into itself must be caught by the same guard."""
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    assert await uow.nodes.is_ancestor_of(a.id, a.id)


async def test_unrelated_nodes_are_not_ancestors(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    b = await add_folder(uow, user, "b", user.root_folder_id)
    assert not await uow.nodes.is_ancestor_of(a.id, b.id)


# --- subtree delete --------------------------------------------------------


async def test_soft_delete_covers_the_whole_subtree(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    b = await add_folder(uow, user, "b", a.id)
    leaf = await add_file(uow, user, "leaf.txt", b.id)

    count = await uow.nodes.soft_delete_subtree(a.id, NOW)
    await uow.flush()

    assert count == 3
    for node_id in (a.id, b.id, leaf.id):
        node = await uow.nodes.get(node_id)
        assert node is not None and node.is_deleted


async def test_soft_delete_leaves_siblings_alone(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    a = await add_folder(uow, user, "a", user.root_folder_id)
    b = await add_folder(uow, user, "b", user.root_folder_id)

    await uow.nodes.soft_delete_subtree(a.id, NOW)
    await uow.flush()

    survivor = await uow.nodes.get(b.id)
    assert survivor is not None and not survivor.is_deleted


async def test_trash_sweep_finds_expired_nodes(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    old = await add_file(uow, user, "old.txt", user.root_folder_id)
    recent = await add_file(uow, user, "recent.txt", user.root_folder_id)
    old.soft_delete(NOW - timedelta(days=40))
    recent.soft_delete(NOW)
    await uow.nodes.update(old)
    await uow.nodes.update(recent)
    await uow.flush()

    expired = await uow.nodes.list_trashed_before(NOW - timedelta(days=30), limit=10)
    assert [n.id for n in expired] == [old.id]


# --- quota -----------------------------------------------------------------


async def test_quota_round_trips(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    await uow.quotas.add(QuotaUsage(user_id=user.id, live_bytes=100, updated_at=NOW))
    await uow.flush()

    usage = await uow.quotas.get(user.id)
    assert usage is not None and usage.live_bytes == 100


async def test_recompute_derives_usage_from_rows(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    await add_file(uow, user, "live.txt", user.root_folder_id, size=500)
    trashed = await add_file(uow, user, "trashed.txt", user.root_folder_id, size=300)
    trashed.soft_delete(NOW)
    await uow.nodes.update(trashed)
    await uow.flush()

    usage = await uow.quotas.recompute(user.id)
    assert usage.live_bytes == 500
    assert usage.trashed_bytes == 300


async def test_recompute_of_an_empty_account_is_zero(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    assert (await uow.quotas.recompute(user.id)).total_bytes == 0


# --- keys ------------------------------------------------------------------


async def test_user_key_round_trips(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    await uow.keys.add_user_key(
        UserKey(
            user_id=user.id,
            wrapped_kek=b"\x00\x01sealed",
            master_key_id="master-v1",
            created_at=NOW,
        )
    )
    await uow.flush()

    key = await uow.keys.get_user_key(user.id)
    assert key is not None
    assert key.wrapped_kek == b"\x00\x01sealed"
    assert key.master_key_id == "master-v1"


async def test_keys_are_listable_by_master_for_rotation(uow: SqlUnitOfWork) -> None:
    alice = await make_user(uow, "alice")
    bob = await make_user(uow, "bob")
    for user, master in ((alice, "master-v1"), (bob, "master-v2")):
        await uow.keys.add_user_key(
            UserKey(
                user_id=user.id,
                wrapped_kek=b"sealed",
                master_key_id=master,
                created_at=NOW,
            )
        )
    await uow.flush()

    pending = await uow.keys.list_user_keys_wrapped_under("master-v1", limit=100)
    assert [k.user_id for k in pending] == [alice.id]


# --- transactions ----------------------------------------------------------


async def test_rollback_discards_writes(uow: SqlUnitOfWork) -> None:
    await make_user(uow, "ephemeral")
    await uow.rollback()
    assert await uow.users.get_by_subject("ephemeral") is None


async def test_commit_persists_across_units(session_factory: object) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory: async_sessionmaker[AsyncSession] = session_factory  # type: ignore[assignment]
    async with SqlUnitOfWork(factory) as first:
        await make_user(first, "durable")
        await first.commit()

    async with SqlUnitOfWork(factory) as second:
        assert await second.users.get_by_subject("durable") is not None


async def test_subtree_lock_is_acquirable(uow: SqlUnitOfWork) -> None:
    """Serializes concurrent moves; released automatically at commit."""
    user = await make_user(uow)
    await uow.lock_subtree(user.root_folder_id)
    await uow.lock_subtree(user.root_folder_id)  # re-entrant within one transaction


# --- concurrency -----------------------------------------------------------


async def test_concurrent_same_name_creation_yields_exactly_one_winner(
    session_factory: object,
) -> None:
    """`file-storage/spec.md`: exactly one succeeds, the other gets a conflict.

    The read-then-write pre-check in the use case can always be raced; the
    partial unique index is the real guarantee, and its violation must surface
    as a domain error rather than a raw driver exception.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from cyberfs.adapters.outbound.db.mappers import node_to_row

    factory: async_sessionmaker[AsyncSession] = session_factory  # type: ignore[assignment]

    async with SqlUnitOfWork(factory) as setup:
        user = await make_user(setup, "racer")
        await setup.commit()
        owner_id, parent_id = user.id, user.root_folder_id

    async def create() -> str:
        async with SqlUnitOfWork(factory) as uow:
            uow.session.add(
                node_to_row(
                    Node(
                        id=uuid.uuid4(),
                        owner_id=owner_id,
                        kind=NodeKind.FILE,
                        name="contested.txt",
                        parent_id=parent_id,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            )
            try:
                await uow.commit()
            except NameTakenError:
                return "conflict"
            return "created"

    outcomes = await asyncio.gather(create(), create())

    assert sorted(outcomes) == ["conflict", "created"]


async def test_encryption_default_survives_a_round_trip(uow: SqlUnitOfWork) -> None:
    user = await make_user(uow)
    node = Node(
        id=uuid.uuid4(),
        owner_id=user.id,
        kind=NodeKind.FOLDER,
        name="secret",
        parent_id=user.root_folder_id,
        created_at=NOW,
        updated_at=NOW,
        encryption_default=EncryptionDefault.ON,
    )
    await uow.nodes.add(node)
    await uow.flush()

    fetched = await uow.nodes.get(node.id)
    assert fetched is not None
    assert fetched.encryption_default is EncryptionDefault.ON


async def test_a_changed_owner_is_persisted(uow: SqlUnitOfWork) -> None:
    """Regression: `apply_node` dropped owner_id, so ownership transfer was
    silently a no-op against the database while passing in-memory."""
    alice = await make_user(uow, "alice")
    bob = await make_user(uow, "bob")
    node = await add_folder(uow, alice, "handover", alice.root_folder_id)

    node.owner_id = bob.id
    await uow.nodes.update(node)
    await uow.commit()

    stored = await uow.nodes.get(node.id)
    assert stored is not None
    assert stored.owner_id == bob.id
