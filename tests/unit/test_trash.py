"""The trash view and emptying it -- `file-storage/spec.md`.

The rules, not the storage: which trashed rows become entries, what an entry
reports, what a restore of one brings back, and what the count guard and the node
budget on emptying refuse. Whether Postgres can answer any of it with an index,
and whether the FK cascades finish the job, belongs to
`tests/integration/test_api_trash.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.application.content import ContentService
from cyberfs.application.jobs import ActivityPruneJob
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.activity import ACTIVITY_ACTIONS, SECURITY_ACTIONS, SUMMARY_BUCKETS
from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import (
    NameTakenError,
    PreconditionFailedError,
    TrashCountMismatchError,
    ValidationError,
)
from cyberfs.domain.nodes import TRASH_PURGE_NODE_BUDGET, Node, NodeKind
from cyberfs.domain.sharing import Grant, Role
from cyberfs.domain.users import User

from .conftest import make_settings
from .fakes import FakeKeyProvider, FakeObjectStore, FakeUnitOfWork, stream

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=2)
LATER = NOW + timedelta(hours=1)
GB = 1024**3
RETENTION_DAYS = 30
PAYLOAD = b"trash-me" * 4


async def provision(uow: FakeUnitOfWork, subject: str = "alice") -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=EARLIER
    )


def nodes(page_size_max: int = 100) -> NodeService:
    return NodeService(
        max_tree_depth=64, page_size_max=page_size_max, trash_retention_days=RETENTION_DAYS
    )


def purge_records(uow: FakeUnitOfWork) -> list[AuditRecord]:
    """Only the records this change writes, so provisioning noise cannot mask them."""
    destructive = {AuditAction.NODE_PURGED, AuditAction.TRASH_EMPTIED}
    return [r for r in uow.audit.records if r.action in destructive]


def content(store: FakeObjectStore) -> ContentService:
    return ContentService(
        store, max_upload_bytes=10 * GB, upload_chunk_bytes=8, version_retention_count=10
    )


# --- classification --------------------------------------------------------


def test_emptying_the_trash_is_a_security_record_not_activity() -> None:
    """Bulk irreversible destruction must outlive the activity around it."""
    assert AuditAction.TRASH_EMPTIED in SECURITY_ACTIONS
    assert AuditAction.TRASH_EMPTIED not in ACTIVITY_ACTIONS


def test_emptying_the_trash_feeds_no_summary_counter() -> None:
    """The per-entry `node.purged` records already describe what happened."""
    assert AuditAction.TRASH_EMPTIED not in SUMMARY_BUCKETS


async def test_an_activity_prune_leaves_the_batch_record_in_place() -> None:
    uow = FakeUnitOfWork()
    ancient = NOW - timedelta(days=400)
    for action in (AuditAction.NODE_DELETED, AuditAction.TRASH_EMPTIED):
        await uow.audit.add(AuditRecord(action=action, occurred_at=ancient, actor_subject="alice"))

    await ActivityPruneJob(make_settings()).run(uow, now=NOW)

    assert [r.action for r in uow.audit.records] == [AuditAction.TRASH_EMPTIED]


def test_the_purge_budget_bounds_nodes_and_is_a_domain_constant() -> None:
    """A setting here would let a deployment lift the bound on an irreversible
    bulk delete, and counting entries would bound nothing at all."""
    assert TRASH_PURGE_NODE_BUDGET > 0
    assert not hasattr(make_settings(), "trash_purge_node_budget")


# --- wiring ----------------------------------------------------------------


async def test_the_configured_retention_window_reaches_the_trash_listing() -> None:
    """`purge_after` is only as trustworthy as the value `create_app` passes.

    `NodeService` defaults the window so the many construction sites that never
    reach the trash need not restate it, which makes this the test that proves the
    default is not silently what production uses.
    """
    app = create_app(make_settings(trash_retention_days=7))
    uow = FakeUnitOfWork()
    user = await provision(uow)
    node = await content(FakeObjectStore()).upload(
        uow, user, user.root_folder_id, "clock.bin", stream(PAYLOAD), now=NOW
    )
    await nodes().delete(uow, user, node.id, now=LATER)

    listing = await app.state.nodes.trash(uow, user, limit=10)

    assert listing.entries[0].purge_after == LATER + timedelta(days=7)


# --- what becomes an entry -------------------------------------------------


async def test_a_trashed_file_is_an_entry_and_a_live_one_is_not() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    files = content(store)
    gone = await files.upload(uow, user, user.root_folder_id, "gone.bin", stream(PAYLOAD), now=NOW)
    await files.upload(uow, user, user.root_folder_id, "here.bin", stream(PAYLOAD), now=NOW)
    await nodes().delete(uow, user, gone.id, now=LATER)

    listing = await nodes().trash(uow, user, limit=10)

    assert [e.node.id for e in listing.entries] == [gone.id]


async def test_a_trashed_root_folder_is_still_not_an_entry() -> None:
    """`Node.soft_delete` refuses a root, so no route can produce such a row.

    The stamp is written straight onto the repository to prove the *listing* also
    excludes it -- otherwise the spec clause "no root folder SHALL appear" would
    rest on a guard one layer away, and this test would pass against an
    implementation with no root handling whatsoever.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    root = await uow.nodes.get(user.root_folder_id)
    assert root is not None
    with pytest.raises(ValidationError):
        root.soft_delete(LATER)
    # Bypassing the domain guard, which is the only thing standing in the way.
    uow.nodes.by_id[root.id] = Node(
        id=root.id,
        owner_id=root.owner_id,
        kind=NodeKind.FOLDER,
        name=root.name,
        parent_id=None,
        created_at=root.created_at,
        updated_at=root.updated_at,
        deleted_at=LATER,
    )

    listing = await nodes().trash(uow, user, limit=10)

    assert listing.entries == ()
    assert listing.total_entries == 0


async def test_a_deleted_folder_yields_exactly_one_entry() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    outer = await svc.create_folder(uow, user, user.root_folder_id, "outer", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "inner", now=NOW)
    await content(store).upload(uow, user, inner.node.id, "a.bin", stream(PAYLOAD), now=NOW)
    await svc.delete(uow, user, outer.node.id, now=LATER)

    listing = await svc.trash(uow, user, limit=10)

    assert [e.node.id for e in listing.entries] == [outer.node.id]
    assert listing.total_entries == 1


async def test_a_node_trashed_inside_a_trashed_folder_becomes_an_entry_on_restore() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = nodes()
    outer = await svc.create_folder(uow, user, user.root_folder_id, "outer", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "inner", now=NOW)
    # `inner` first, so it carries its own stamp and is not lifted with `outer`.
    await svc.delete(uow, user, inner.node.id, now=NOW)
    await svc.delete(uow, user, outer.node.id, now=LATER)

    folded = await svc.trash(uow, user, limit=10)
    assert [e.node.id for e in folded.entries] == [outer.node.id]

    await svc.restore(uow, user, outer.node.id, now=LATER)

    surfaced = await svc.trash(uow, user, limit=10)
    assert [e.node.id for e in surfaced.entries] == [inner.node.id]


async def test_another_users_trash_is_never_listed() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    store = FakeObjectStore()
    node = await content(store).upload(
        uow, alice, alice.root_folder_id, "alice.bin", stream(PAYLOAD), now=NOW
    )
    # A recipient at delete time still sees nothing: a soft delete withdraws it.
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=node.id,
            subject=bob.subject,
            role=Role.EDITOR,
            granted_by=alice.subject,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await nodes().delete(uow, alice, node.id, now=LATER)

    bobs = await nodes().trash(uow, bob, limit=10)
    assert bobs.entries == ()
    assert bobs.total_entries == 0
    assert [e.node.id for e in (await nodes().trash(uow, alice, limit=10)).entries] == [node.id]


# --- what an entry reports -------------------------------------------------


async def test_an_entry_reports_the_whole_subtree_not_the_folders_own_zero() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    files = content(store)
    outer = await svc.create_folder(uow, user, user.root_folder_id, "outer", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "inner", now=NOW)
    await files.upload(uow, user, outer.node.id, "a.bin", stream(PAYLOAD), now=NOW)
    await files.upload(uow, user, inner.node.id, "b.bin", stream(PAYLOAD), now=NOW)
    await svc.delete(uow, user, outer.node.id, now=LATER)

    entry = (await svc.trash(uow, user, limit=10)).entries[0]

    assert entry.node.size_bytes == 0, "the folder row itself holds no bytes"
    assert entry.totals.size_bytes == 2 * len(PAYLOAD)
    assert entry.totals.nodes == 4  # outer, inner, a.bin, b.bin


async def test_an_entrys_bytes_are_the_current_version_only() -> None:
    """The figure matches the trashed quota bucket, not what a purge would free.

    A purge also destroys every retained version's object, so `bytes_reclaimed`
    from purging this entry will exceed what the entry reports. That is the
    documented choice -- the entry describes the bucket the delete moved -- and
    this pins it rather than leaving it incidental.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    files = content(store)
    first = await files.upload(
        uow, user, user.root_folder_id, "v.bin", stream(PAYLOAD), now=EARLIER
    )
    await files.upload(uow, user, user.root_folder_id, "v.bin", stream(PAYLOAD * 3), now=NOW)
    await nodes().delete(uow, user, first.id, now=LATER)

    entry = (await nodes().trash(uow, user, limit=10)).entries[0]

    assert len(await uow.versions.list_for_node(first.id)) == 2
    assert entry.totals.size_bytes == 3 * len(PAYLOAD)


async def test_an_entrys_totals_exclude_a_separately_deleted_descendant() -> None:
    """The total describes what restoring brings back, not what sits beneath.

    A child trashed on its own occasion stays trashed through the restore, so
    counting it here would promise bytes the restore does not return -- and would
    count them twice once the child becomes its own entry.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    files = content(store)
    folder = await svc.create_folder(uow, user, user.root_folder_id, "folder", now=NOW)
    await files.upload(uow, user, folder.node.id, "kept.bin", stream(PAYLOAD), now=NOW)
    dropped = await files.upload(uow, user, folder.node.id, "dropped.bin", stream(PAYLOAD), now=NOW)
    await svc.delete(uow, user, dropped.id, now=NOW)
    await svc.delete(uow, user, folder.node.id, now=LATER)

    entry = (await svc.trash(uow, user, limit=10)).entries[0]

    assert entry.node.id == folder.node.id
    assert entry.totals.nodes == 2  # the folder and `kept.bin`
    assert entry.totals.size_bytes == len(PAYLOAD)


async def test_an_entry_reports_the_path_it_came_from() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    reports = await svc.create_folder(uow, user, user.root_folder_id, "reports", now=NOW)
    node = await content(store).upload(
        uow, user, reports.node.id, "q3.xlsx", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, user, node.id, now=LATER)

    entry = (await svc.trash(uow, user, limit=10)).entries[0]

    assert entry.path == "/reports/q3.xlsx"


async def test_an_entry_reports_its_retention_deadline() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    node = await content(store).upload(
        uow, user, user.root_folder_id, "clock.bin", stream(PAYLOAD), now=NOW
    )
    await nodes().delete(uow, user, node.id, now=LATER)

    entry = (await nodes().trash(uow, user, limit=10)).entries[0]

    assert entry.deleted_at == LATER
    assert entry.purge_after == LATER + timedelta(days=RETENTION_DAYS)


async def trashed_files(uow: FakeUnitOfWork, user: User, count: int) -> list[uuid.UUID]:
    """`count` files, each deleted a minute after the last."""
    svc = nodes()
    files = content(FakeObjectStore())
    order = []
    for index in range(count):
        node = await files.upload(
            uow, user, user.root_folder_id, f"f{index}.bin", stream(PAYLOAD), now=NOW
        )
        await svc.delete(uow, user, node.id, now=NOW + timedelta(minutes=index))
        order.append(node.id)
    return order


async def test_the_listing_is_newest_first_bounded_and_full_while_more_remain() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    order = await trashed_files(uow, user, 5)
    svc = nodes()

    first = await svc.trash(uow, user, limit=2)
    assert [e.node.id for e in first.entries] == list(reversed(order))[:2]
    assert first.next_cursor is not None

    second = await svc.trash(uow, user, limit=2, cursor=first.next_cursor)
    assert [e.node.id for e in second.entries] == list(reversed(order))[2:4]

    third = await svc.trash(uow, user, limit=2, cursor=second.next_cursor)
    assert [e.node.id for e in third.entries] == list(reversed(order))[4:]
    assert third.next_cursor is None


async def test_the_total_counts_the_whole_trash_on_every_page() -> None:
    """The number the purge guard needs, obtainable from any single request."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    await trashed_files(uow, user, 5)
    svc = nodes()

    first = await svc.trash(uow, user, limit=2)
    assert len(first.entries) == 2
    assert first.total_entries == 5

    assert first.next_cursor is not None
    second = await svc.trash(uow, user, limit=2, cursor=first.next_cursor)
    assert second.total_entries == 5


async def test_a_cursor_from_another_walk_is_refused_rather_than_misread() -> None:
    """A keyset cursor names a position in one ordered result set.

    Presented against a different one it describes nothing, and serving it would
    silently drop a prefix of the results. Refused as a validation failure -- the
    `422` every other paginated surface answers -- not raised out of a date parse
    as a `500`.
    """
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    await trashed_files(uow, alice, 3)
    await trashed_files(uow, bob, 3)
    svc = nodes()

    alices = await svc.trash(uow, alice, limit=1)
    assert alices.next_cursor is not None

    with pytest.raises(ValidationError):
        await svc.trash(uow, bob, limit=1, cursor=alices.next_cursor)
    with pytest.raises(ValidationError):
        await svc.trash(uow, alice, limit=1, cursor="not-a-cursor-at-all")
    with pytest.raises(ValidationError):
        await svc.trash(uow, alice, limit=1, cursor=alices.next_cursor[:-4])


async def test_the_page_is_bounded_by_the_configured_maximum() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    await trashed_files(uow, user, 3)

    listing = await nodes(page_size_max=2).trash(uow, user, limit=1000)

    assert len(listing.entries) == 2
    assert listing.total_entries == 3


# --- restoring an entry ----------------------------------------------------


async def test_restoring_a_folder_clears_every_descendant_the_delete_trashed() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    outer = await svc.create_folder(uow, user, user.root_folder_id, "outer", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "inner", now=NOW)
    leaf = await content(store).upload(
        uow, user, inner.node.id, "leaf.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, user, outer.node.id, now=LATER)

    entry = (await svc.trash(uow, user, limit=10)).entries[0]
    await svc.restore(uow, user, entry.node.id, now=LATER)

    for node_id in (outer.node.id, inner.node.id, leaf.id):
        node = await uow.nodes.get(node_id)
        assert node is not None and not node.is_deleted
    assert (await svc.trash(uow, user, limit=10)).entries == ()


async def test_a_separately_deleted_child_stays_trashed_when_its_parent_returns() -> None:
    """The delete refused to re-stamp it; the restore must not override that."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    folder = await svc.create_folder(uow, user, user.root_folder_id, "folder", now=NOW)
    kept = await content(store).upload(
        uow, user, folder.node.id, "kept.bin", stream(PAYLOAD), now=NOW
    )
    dropped = await content(store).upload(
        uow, user, folder.node.id, "dropped.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, user, dropped.id, now=NOW)
    await svc.delete(uow, user, folder.node.id, now=LATER)

    await svc.restore(uow, user, folder.node.id, now=LATER)

    still_gone = await uow.nodes.get(dropped.id)
    assert still_gone is not None and still_gone.is_deleted
    back = await uow.nodes.get(kept.id)
    assert back is not None and not back.is_deleted
    assert [e.node.id for e in (await svc.trash(uow, user, limit=10)).entries] == [dropped.id]


async def test_restore_moves_out_exactly_the_bytes_it_made_visible() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    folder = await svc.create_folder(uow, user, user.root_folder_id, "folder", now=NOW)
    await content(store).upload(uow, user, folder.node.id, "kept.bin", stream(PAYLOAD), now=NOW)
    dropped = await content(store).upload(
        uow, user, folder.node.id, "dropped.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, user, dropped.id, now=NOW)
    await svc.delete(uow, user, folder.node.id, now=LATER)

    await svc.restore(uow, user, folder.node.id, now=LATER)

    usage = await uow.quotas.get(user.id)
    recomputed = await uow.quotas.recompute(user.id)
    assert usage is not None
    # The rows and the buckets agree: only `dropped.bin` is still trashed.
    assert usage.trashed_bytes == recomputed.trashed_bytes == len(PAYLOAD)
    assert usage.live_bytes == recomputed.live_bytes == len(PAYLOAD)


async def test_every_restored_rows_revision_advanced() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    folder = await svc.create_folder(uow, user, user.root_folder_id, "folder", now=NOW)
    leaf = await content(store).upload(
        uow, user, folder.node.id, "leaf.bin", stream(PAYLOAD), now=NOW
    )
    stale_etag = leaf.etag

    await svc.delete(uow, user, folder.node.id, now=LATER)
    await svc.restore(uow, user, folder.node.id, now=LATER)

    with pytest.raises(PreconditionFailedError):
        await svc.rename(uow, user, leaf.id, "renamed.bin", if_match=stale_etag, now=LATER)


async def test_restoring_beneath_a_still_trashed_parent_lands_in_the_root() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = nodes()
    outer = await svc.create_folder(uow, user, user.root_folder_id, "outer", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "inner", now=NOW)
    await svc.delete(uow, user, inner.node.id, now=NOW)
    await svc.delete(uow, user, outer.node.id, now=LATER)

    view = await svc.restore(uow, user, inner.node.id, now=LATER)

    assert view.node.parent_id == user.root_folder_id


async def test_a_restore_onto_a_taken_name_is_refused_and_lifts_nothing() -> None:
    """ "Name reusable after deletion" lets a live sibling take the name.

    Characterization, not regression: `_ensure_name_free` already refused this,
    but no caller could learn a trashed id, so it was unreachable. The listing
    makes it routine, and the refusal must leave the whole subtree trashed rather
    than half-lifted.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    folder = await svc.create_folder(uow, user, user.root_folder_id, "folder", now=NOW)
    leaf = await content(store).upload(
        uow, user, folder.node.id, "leaf.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, user, folder.node.id, now=LATER)
    await svc.create_folder(uow, user, user.root_folder_id, "folder", now=LATER)

    with pytest.raises(NameTakenError):
        await svc.restore(uow, user, folder.node.id, now=LATER)

    for node_id in (folder.node.id, leaf.id):
        still = await uow.nodes.get(node_id)
        assert still is not None and still.is_deleted, "the refusal lifted part of the subtree"


class RecordingCache:
    """Records what an invalidation touched, without a Redis.

    Only the methods `NodeService` reaches; anything else raises, so a new
    invalidation path shows up as a failure rather than as silence.
    """

    def __init__(self) -> None:
        self.nodes: list[uuid.UUID] = []
        self.listings: list[uuid.UUID | None] = []
        self.all_permissions = 0

    async def permission(
        self,
        subject: str,
        node_id: uuid.UUID,
        load: Callable[[], Awaitable[Role | None]],
    ) -> Role | None:
        # Never actually caches, so an authorization decision here is always the
        # live one and cannot mask a missing invalidation.
        return await load()

    async def on_node_mutated(
        self,
        node_id: uuid.UUID,
        *,
        old_parent: uuid.UUID | None = None,
        new_parent: uuid.UUID | None = None,
    ) -> None:
        self.nodes.append(node_id)
        self.listings.append(old_parent)
        if new_parent is not None:
            self.listings.append(new_parent)

    async def invalidate_node(self, node_id: uuid.UUID) -> None:
        self.nodes.append(node_id)

    async def invalidate_listing(self, parent_id: uuid.UUID | None) -> None:
        self.listings.append(parent_id)

    async def invalidate_all_permissions(self) -> None:
        self.all_permissions += 1


async def test_a_subtree_restore_invalidates_every_row_it_lifted() -> None:
    """A restore is a subtree mutation, so `caching/spec.md` wants the subtree.

    Regression test for the entry-shaped invalidation the cascading restore left
    behind: without the fix the descendants' node keys and the entry's own
    children listing survive the restore.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    cache = RecordingCache()
    svc = NodeService(
        max_tree_depth=64,
        page_size_max=100,
        trash_retention_days=RETENTION_DAYS,
        cache=cache,  # type: ignore[arg-type]
    )
    outer = await svc.create_folder(uow, user, user.root_folder_id, "outer", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "inner", now=NOW)
    leaf = await content(FakeObjectStore()).upload(
        uow, user, inner.node.id, "leaf.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, user, outer.node.id, now=LATER)
    cache.nodes.clear()
    cache.listings.clear()

    await svc.restore(uow, user, outer.node.id, now=LATER)

    for node_id in (outer.node.id, inner.node.id, leaf.id):
        assert node_id in cache.nodes, f"the restore left {node_id} cached"
    assert outer.node.id in cache.listings, "the entry's own children listing survived"
    assert cache.all_permissions >= 1


# --- emptying the trash ----------------------------------------------------


async def empty_world(
    subject: str = "alice", entries: int = 2
) -> tuple[FakeUnitOfWork, User, FakeObjectStore, list[uuid.UUID]]:
    uow = FakeUnitOfWork()
    user = await provision(uow, subject)
    store = FakeObjectStore()
    svc = nodes()
    files = content(store)
    trashed = []
    for index in range(entries):
        node = await files.upload(
            uow, user, user.root_folder_id, f"e{index}.bin", stream(PAYLOAD), now=NOW
        )
        await svc.delete(uow, user, node.id, now=NOW + timedelta(minutes=index))
        trashed.append(node.id)
    return uow, user, store, trashed


async def test_a_matching_count_purges_every_entry_and_frees_the_bytes() -> None:
    uow, user, store, trashed = await empty_world(entries=3)

    result = await nodes().empty_trash(uow, user, expected_entries=3, objects=store, now=LATER)

    assert result.entries_purged == 3
    assert result.entries_remaining == 0
    assert result.purged.bytes_reclaimed == 3 * len(PAYLOAD)
    assert result.purged.objects_deleted == 3
    for node_id in trashed:
        assert await uow.nodes.get(node_id) is None
    usage = await uow.quotas.get(user.id)
    recomputed = await uow.quotas.recompute(user.id)
    assert usage is not None
    # Against the recomputation, not against zero: `purge_from_trash` floors at
    # zero, so an over-release would be invisible to `trashed_bytes == 0`.
    assert usage.trashed_bytes == recomputed.trashed_bytes
    assert usage.live_bytes == recomputed.live_bytes
    assert store.objects == {}


async def test_emptying_an_entry_holding_an_older_deletion_keeps_the_buckets_honest() -> None:
    """The riskiest arithmetic: `_strip` runs once per row across two batches.

    The folder's own delete batch excludes `dropped.bin`, which was trashed
    earlier -- but purging the folder destroys it too, because it sits in the
    subtree. Both entries' bytes must be released exactly once, which only a
    recomputation can show.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    files = content(store)
    folder = await svc.create_folder(uow, user, user.root_folder_id, "folder", now=NOW)
    kept = await files.upload(uow, user, folder.node.id, "kept.bin", stream(PAYLOAD), now=NOW)
    dropped = await files.upload(uow, user, folder.node.id, "dropped.bin", stream(PAYLOAD), now=NOW)
    await svc.delete(uow, user, dropped.id, now=NOW)
    await svc.delete(uow, user, folder.node.id, now=LATER)

    result = await svc.empty_trash(uow, user, expected_entries=1, objects=store, now=LATER)

    assert result.entries_purged == 1
    assert result.purged.nodes_deleted == 3
    for node_id in (folder.node.id, kept.id, dropped.id):
        assert await uow.nodes.get(node_id) is None
    usage = await uow.quotas.get(user.id)
    recomputed = await uow.quotas.recompute(user.id)
    assert usage is not None
    assert usage.trashed_bytes == recomputed.trashed_bytes == 0
    assert usage.live_bytes == recomputed.live_bytes == 0
    assert store.objects == {}


async def test_a_stale_count_is_refused_and_destroys_nothing() -> None:
    uow, user, store, trashed = await empty_world(entries=2)

    with pytest.raises(TrashCountMismatchError, match="stated number"):
        await nodes().empty_trash(uow, user, expected_entries=1, objects=store, now=LATER)

    for node_id in trashed:
        assert await uow.nodes.get(node_id) is not None
    usage = await uow.quotas.get(user.id)
    assert usage is not None
    assert usage.trashed_bytes == 2 * len(PAYLOAD)
    assert store.deleted == []
    assert purge_records(uow) == []


async def test_the_count_mismatch_carries_its_own_error_code() -> None:
    """A bare `409` is indistinguishable from `name_taken` to a client."""
    uow, user, store, _ = await empty_world(entries=1)

    with pytest.raises(TrashCountMismatchError) as raised:
        await nodes().empty_trash(uow, user, expected_entries=9, objects=store, now=LATER)

    assert raised.value.code == "trash_count_mismatch"
    assert raised.value.context == {"expected_entries": 9, "entries": 1}


async def test_an_empty_trash_with_a_count_of_zero_succeeds() -> None:
    uow, user, store, _ = await empty_world(entries=0)

    result = await nodes().empty_trash(uow, user, expected_entries=0, objects=store, now=LATER)

    assert result.entries_purged == 0
    assert result.entries_remaining == 0
    assert result.purged.nodes_deleted == 0
    # `TRASH_EMPTIED` is never pruned, so a no-op must not write one: a client
    # looping on this endpoint would otherwise grow the retained security log
    # without bound with rows recording nothing.
    assert purge_records(uow) == []


async def deep_entry(
    uow: FakeUnitOfWork, user: User, store: FakeObjectStore, name: str, files: int, when: datetime
) -> uuid.UUID:
    """A folder holding `files` files, trashed at `when`. Costs `files + 1` nodes."""
    svc = nodes()
    folder = await svc.create_folder(uow, user, user.root_folder_id, name, now=EARLIER)
    for index in range(files):
        await content(store).upload(
            uow, user, folder.node.id, f"{name}-{index}.bin", stream(PAYLOAD), now=EARLIER
        )
    await svc.delete(uow, user, folder.node.id, now=when)
    return folder.node.id


async def test_the_call_stops_before_an_entry_that_would_break_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-destroyed entry would still list, with totals it no longer matches."""
    monkeypatch.setattr("cyberfs.application.nodes.TRASH_PURGE_NODE_BUDGET", 5)
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    first = await deep_entry(uow, user, store, "first", files=3, when=NOW)
    second = await deep_entry(uow, user, store, "second", files=3, when=LATER)

    result = await nodes().empty_trash(uow, user, expected_entries=2, objects=store, now=LATER)

    assert result.entries_purged == 1
    assert result.entries_remaining == 1
    assert result.purged.nodes_deleted == 4
    # Oldest deletion first, so the survivor is the newer entry -- entirely.
    assert await uow.nodes.get(first) is None
    survivor = await uow.nodes.get(second)
    assert survivor is not None
    listing = await nodes().trash(uow, user, limit=10)
    assert [e.node.id for e in listing.entries] == [second]
    assert listing.entries[0].totals.nodes == 4, "the survivor was partly destroyed"


async def test_a_second_call_with_the_reported_count_finishes_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The number the response reports is the number the next call states."""
    monkeypatch.setattr("cyberfs.application.nodes.TRASH_PURGE_NODE_BUDGET", 5)
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    await deep_entry(uow, user, store, "first", files=3, when=NOW)
    await deep_entry(uow, user, store, "second", files=3, when=LATER)
    svc = nodes()

    first = await svc.empty_trash(uow, user, expected_entries=2, objects=store, now=LATER)
    second = await svc.empty_trash(
        uow, user, expected_entries=first.entries_remaining, objects=store, now=LATER
    )

    assert second.entries_purged == 1
    assert second.entries_remaining == 0
    assert (await svc.trash(uow, user, limit=10)).entries == ()
    assert store.objects == {}


async def test_an_oldest_entry_larger_than_the_budget_is_still_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise no sequence of calls could ever empty such a trash."""
    monkeypatch.setattr("cyberfs.application.nodes.TRASH_PURGE_NODE_BUDGET", 2)
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    oversized = await deep_entry(uow, user, store, "oversized", files=5, when=NOW)

    result = await nodes().empty_trash(uow, user, expected_entries=1, objects=store, now=LATER)

    assert result.entries_purged == 1
    assert result.purged.nodes_deleted == 6
    assert await uow.nodes.get(oversized) is None
    assert result.entries_remaining == 0


async def test_the_batch_and_every_entry_are_recorded() -> None:
    uow, user, store, trashed = await empty_world(entries=2)

    await nodes().empty_trash(uow, user, expected_entries=2, objects=store, now=LATER)

    purged = [r for r in uow.audit.records if r.action is AuditAction.NODE_PURGED]
    batch = [r for r in uow.audit.records if r.action is AuditAction.TRASH_EMPTIED]
    assert {r.target_id for r in purged} == {str(n) for n in trashed}
    assert len(batch) == 1
    assert batch[0].context["entries"] == 2
    assert batch[0].context["bytes"] == 2 * len(PAYLOAD)
    # The batch is not a node, so it names none.
    assert batch[0].target_id is None


async def test_one_record_per_entry_not_per_node_and_shaped_like_a_purge() -> None:
    """The same shape `POST /nodes/{id}/purge` writes, so the trail cannot drift."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    entry = await deep_entry(uow, user, store, "tree", files=3, when=NOW)

    await nodes().empty_trash(uow, user, expected_entries=1, objects=store, now=LATER)

    purged = [r for r in uow.audit.records if r.action is AuditAction.NODE_PURGED]
    assert len(purged) == 1, "one record per entry, not one per node"
    assert purged[0].target_id == str(entry)
    assert purged[0].context["nodes"] == 4
    assert purged[0].context["objects"] == 3
    assert purged[0].context["owner_id"] == str(user.id)


async def test_a_solitary_purge_writes_the_same_record_as_emptying_does() -> None:
    """Both paths reach one emitter, so neither can drift from the other."""
    uow, user, store, trashed = await empty_world(entries=1)
    svc = nodes()

    await svc.purge(uow, user, trashed[0], objects=store, now=LATER)
    solitary = [r for r in uow.audit.records if r.action is AuditAction.NODE_PURGED]

    other, owner, its_store, _ids = await empty_world(subject="bob", entries=1)
    await svc.empty_trash(other, owner, expected_entries=1, objects=its_store, now=LATER)
    batched = [r for r in other.audit.records if r.action is AuditAction.NODE_PURGED]

    assert len(solitary) == len(batched) == 1
    assert solitary[0].context.keys() == batched[0].context.keys()


async def test_emptying_one_trash_leaves_another_users_untouched() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    store = FakeObjectStore()
    svc = nodes()
    files = content(store)
    mine = await files.upload(
        uow, alice, alice.root_folder_id, "mine.bin", stream(PAYLOAD), now=NOW
    )
    theirs = await files.upload(
        uow, bob, bob.root_folder_id, "theirs.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, alice, mine.id, now=LATER)
    await svc.delete(uow, bob, theirs.id, now=LATER)

    result = await svc.empty_trash(uow, alice, expected_entries=1, objects=store, now=LATER)

    assert result.entries_purged == 1
    assert await uow.nodes.get(mine.id) is None
    assert await uow.nodes.get(theirs.id) is not None
    bobs_usage = await uow.quotas.get(bob.id)
    assert bobs_usage is not None and bobs_usage.trashed_bytes == len(PAYLOAD)


async def test_a_live_node_is_never_destroyed() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    files = content(store)
    folder = await svc.create_folder(uow, user, user.root_folder_id, "folder", now=NOW)
    live = await files.upload(uow, user, folder.node.id, "live.bin", stream(PAYLOAD), now=NOW)
    doomed = await files.upload(uow, user, folder.node.id, "doomed.bin", stream(PAYLOAD), now=NOW)
    await svc.delete(uow, user, doomed.id, now=LATER)

    await svc.empty_trash(uow, user, expected_entries=1, objects=store, now=LATER)

    assert await uow.nodes.get(live.id) is not None
    assert await uow.nodes.get(folder.node.id) is not None
    assert await uow.nodes.get(doomed.id) is None


async def test_emptying_destroys_a_trashed_folders_whole_subtree() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    files = content(store)
    outer = await svc.create_folder(uow, user, user.root_folder_id, "outer", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "inner", now=NOW)
    leaf = await files.upload(uow, user, inner.node.id, "leaf.bin", stream(PAYLOAD), now=NOW)
    await svc.delete(uow, user, outer.node.id, now=LATER)

    result = await svc.empty_trash(uow, user, expected_entries=1, objects=store, now=LATER)

    assert result.purged.nodes_deleted == 3
    assert result.purged.objects_deleted == 1
    for node_id in (outer.node.id, inner.node.id, leaf.id):
        assert await uow.nodes.get(node_id) is None
    assert store.objects == {}
