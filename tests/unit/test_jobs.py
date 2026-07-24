"""Purge, orphan reaper, and quota reconciliation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.content import ContentService
from cyberfs.application.jobs import (
    OrphanReaper,
    PurgeJob,
    ReconcileQuotasJob,
    parse_object_key,
)
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.sharing import Grant, Role
from cyberfs.domain.users import User

from .conftest import make_settings
from .fakes import FakeKeyProvider, FakeObjectStore, FakeUnitOfWork, stream

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
GB = 1024**3
PAYLOAD = b"payload-bytes" * 4


def settings(**kw: object):
    return make_settings(**kw)


async def provision(uow: FakeUnitOfWork, subject: str = "alice") -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


def content(store: FakeObjectStore) -> ContentService:
    return ContentService(
        store, max_upload_bytes=10 * GB, upload_chunk_bytes=8, version_retention_count=10
    )


# --- object keys -----------------------------------------------------------


def test_a_well_formed_key_is_parsed() -> None:
    owner, node, version = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    parsed = parse_object_key(f"{owner}/{node}/{version}")
    assert parsed == (node, version)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "not-a-key",
        "only/two",
        "a/b/c/d",
        "backup/2026/dump.sql",
        "notauuid/notauuid/notauuid",
    ],
)
def test_foreign_keys_are_not_parsed(key: str) -> None:
    assert parse_object_key(key) is None


# --- purge -----------------------------------------------------------------


async def test_purge_removes_trash_past_its_retention() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    files = content(store)
    nodes = NodeService(max_tree_depth=64, page_size_max=100)

    node = await files.upload(uow, user, user.root_folder_id, "old.bin", stream(PAYLOAD), now=NOW)
    await nodes.delete(uow, user, node.id, now=NOW)

    result = await PurgeJob(store, settings(trash_retention_days=30)).run(
        uow, now=NOW + timedelta(days=40)
    )

    assert result.nodes_purged == 1
    assert await uow.nodes.get(node.id) is None


async def test_purge_leaves_recent_trash_alone() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    node = await content(store).upload(
        uow, user, user.root_folder_id, "recent.bin", stream(PAYLOAD), now=NOW
    )
    await nodes.delete(uow, user, node.id, now=NOW)

    result = await PurgeJob(store, settings(trash_retention_days=30)).run(
        uow, now=NOW + timedelta(days=1)
    )

    assert result.nodes_purged == 0
    assert await uow.nodes.get(node.id) is not None


async def test_purge_deletes_the_objects_too() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    node = await content(store).upload(
        uow, user, user.root_folder_id, "old.bin", stream(PAYLOAD), now=NOW
    )
    await nodes.delete(uow, user, node.id, now=NOW)

    result = await PurgeJob(store, settings(trash_retention_days=30)).run(
        uow, now=NOW + timedelta(days=40)
    )

    assert result.objects_deleted == 1
    assert store.objects == {}


async def test_purge_is_the_transition_that_frees_quota() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    node = await content(store).upload(
        uow, user, user.root_folder_id, "old.bin", stream(PAYLOAD), now=NOW
    )
    await nodes.delete(uow, user, node.id, now=NOW)

    trashed = await uow.quotas.get(user.id)
    assert trashed is not None and trashed.total_bytes == len(PAYLOAD)

    await PurgeJob(store, settings(trash_retention_days=30)).run(uow, now=NOW + timedelta(days=40))

    freed = await uow.quotas.get(user.id)
    assert freed is not None and freed.total_bytes == 0


async def test_purge_removes_grants_and_wrapped_keys() -> None:
    """A purged node must leave no route back for a former recipient."""
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    store = FakeObjectStore()
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    node = await content(store).upload(
        uow, alice, alice.root_folder_id, "shared.bin", stream(PAYLOAD), now=NOW
    )
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=node.id,
            subject="bob",
            role=Role.VIEWER,
            granted_by="alice",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await nodes.delete(uow, alice, node.id, now=NOW)

    await PurgeJob(store, settings(trash_retention_days=30)).run(uow, now=NOW + timedelta(days=40))

    assert await uow.grants.list_for_node(node.id) == ()


# --- orphan reaper ---------------------------------------------------------


async def test_an_unreferenced_object_is_reaped() -> None:
    uow = FakeUnitOfWork()
    store = FakeObjectStore()
    key = f"{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}"
    store.objects[key] = b"orphaned"
    store.timestamps[key] = NOW - timedelta(hours=2)

    result = await OrphanReaper(store, settings(orphan_grace_minutes=60)).run(uow, now=NOW)

    assert result.objects_deleted == 1
    assert store.objects == {}


async def test_a_referenced_object_survives() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    node = await content(store).upload(
        uow, user, user.root_folder_id, "live.bin", stream(PAYLOAD), now=NOW
    )
    key = f"{node.owner_id}/{node.id}/{node.current_version_id}"
    store.timestamps[key] = NOW - timedelta(hours=2)

    result = await OrphanReaper(store, settings(orphan_grace_minutes=60)).run(uow, now=NOW)

    assert result.objects_deleted == 0
    assert key in store.objects


async def test_an_object_inside_the_grace_period_is_spared() -> None:
    """An upload in flight -- object written, row not yet committed."""
    uow = FakeUnitOfWork()
    store = FakeObjectStore()
    key = f"{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}"
    store.objects[key] = b"still-uploading"
    store.timestamps[key] = NOW - timedelta(minutes=5)

    result = await OrphanReaper(store, settings(orphan_grace_minutes=60)).run(uow, now=NOW)

    assert result.objects_deleted == 0


async def test_an_object_of_unknown_age_is_spared() -> None:
    """Leaving garbage is far better than deleting live data."""
    uow = FakeUnitOfWork()
    store = FakeObjectStore()
    key = f"{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}"
    store.objects[key] = b"no-timestamp"

    result = await OrphanReaper(store, settings(orphan_grace_minutes=60)).run(uow, now=NOW)

    assert result.objects_deleted == 0


async def test_a_foreign_key_shape_is_never_reaped() -> None:
    """The reaper must not be what removes another system's data."""
    uow = FakeUnitOfWork()
    store = FakeObjectStore()
    store.objects["backups/2026-07-24/dump.sql"] = b"someone-elses-data"
    store.timestamps["backups/2026-07-24/dump.sql"] = NOW - timedelta(days=30)

    result = await OrphanReaper(store, settings(orphan_grace_minutes=60)).run(uow, now=NOW)

    assert result.objects_deleted == 0
    assert "backups/2026-07-24/dump.sql" in store.objects


async def test_the_reaper_reports_what_it_scanned() -> None:
    uow = FakeUnitOfWork()
    store = FakeObjectStore()
    for _ in range(3):
        key = f"{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}"
        store.objects[key] = b"x"
        store.timestamps[key] = NOW - timedelta(hours=2)

    result = await OrphanReaper(store, settings(orphan_grace_minutes=60)).run(uow, now=NOW)

    assert result.objects_scanned == 3
    assert result.bytes_reclaimed == 3


# --- reconciliation --------------------------------------------------------


async def test_reconcile_corrects_a_drifted_counter() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    usage = await uow.quotas.get(user.id)
    assert usage is not None
    usage.live_bytes = 999_999  # drift, as if a release was lost

    result = await ReconcileQuotasJob(settings()).run(uow, now=NOW)

    assert result.users_checked == 1
    assert result.users_corrected == 1


async def test_reconcile_leaves_a_correct_counter_alone() -> None:
    uow = FakeUnitOfWork()
    await provision(uow)

    result = await ReconcileQuotasJob(settings()).run(uow, now=NOW)

    assert result.users_checked == 1
    assert result.users_corrected == 0


async def test_reconcile_creates_a_missing_counter() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    uow.quotas.by_user.clear()

    result = await ReconcileQuotasJob(settings()).run(uow, now=NOW)

    assert result.users_corrected == 1
    assert await uow.quotas.get(user.id) is not None


async def test_reconcile_covers_every_user() -> None:
    uow = FakeUnitOfWork()
    await provision(uow, "alice")
    await provision(uow, "bob")

    result = await ReconcileQuotasJob(settings()).run(uow, now=NOW)

    assert result.users_checked == 2
