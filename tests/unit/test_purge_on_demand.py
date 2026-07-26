"""On-demand purge: `NodeService.purge`.

Purge is irreversible, so the properties worth testing are the guards as much
as the destruction: that a live node survives, that a non-owner is refused, and
that a folder does not leave its descendants' objects behind in the store.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.content import ContentService
from cyberfs.application.jobs import PurgeJob
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.activity import ACTIVITY_ACTIONS, SECURITY_ACTIONS, SUMMARY_BUCKETS
from cyberfs.domain.audit import AuditAction
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import ConflictError, NotFoundError
from cyberfs.domain.sharing import Grant, Role
from cyberfs.domain.users import User

from .conftest import make_settings
from .fakes import FakeKeyProvider, FakeObjectStore, FakeUnitOfWork, stream

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3
PAYLOAD = b"purge-me" * 8


async def provision(uow: FakeUnitOfWork, subject: str = "alice") -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


def content(store: FakeObjectStore) -> ContentService:
    return ContentService(
        store, max_upload_bytes=10 * GB, upload_chunk_bytes=8, version_retention_count=10
    )


def nodes() -> NodeService:
    return NodeService(max_tree_depth=64, page_size_max=100)


# --- classification --------------------------------------------------------


def test_purge_is_a_security_action_not_an_activity_one() -> None:
    """It must survive an activity prune, so a purge stays attributable."""
    assert AuditAction.NODE_PURGED in SECURITY_ACTIONS
    assert AuditAction.NODE_PURGED not in ACTIVITY_ACTIONS


def test_purge_feeds_no_summary_counter() -> None:
    """The soft delete already counted; a purge is not a second deletion."""
    assert AuditAction.NODE_PURGED not in SUMMARY_BUCKETS


# --- guards ----------------------------------------------------------------


async def test_a_live_node_cannot_be_purged() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    node = await content(store).upload(
        uow, user, user.root_folder_id, "live.bin", stream(PAYLOAD), now=NOW
    )

    with pytest.raises(ConflictError, match="trash"):
        await svc.purge(uow, user, node.id, objects=store, now=LATER)

    # Nothing destroyed.
    assert await uow.nodes.get(node.id) is not None
    assert store.deleted == []


async def test_an_unknown_node_is_not_found() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    with pytest.raises(NotFoundError):
        await nodes().purge(uow, user, uuid.uuid4(), objects=FakeObjectStore(), now=LATER)


async def test_purging_twice_reports_not_found_rather_than_success() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    node = await content(store).upload(
        uow, user, user.root_folder_id, "once.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, user, node.id, now=NOW)
    await svc.purge(uow, user, node.id, objects=store, now=LATER)

    with pytest.raises(NotFoundError):
        await svc.purge(uow, user, node.id, objects=store, now=LATER)


async def test_a_share_recipient_cannot_purge() -> None:
    """An editor grant confers no right to destroy content.

    Refused as `NotFoundError`, not `PermissionDeniedError`: a soft delete has
    already revoked recipients' access, so the caller holds no role at all and
    `require_role` declines to disclose that the node exists. Same treatment
    `restore` gives, where only ownership counts.
    """
    uow = FakeUnitOfWork()
    owner = await provision(uow, "alice")
    other = await provision(uow, "bob")
    store = FakeObjectStore()
    svc = nodes()
    node = await content(store).upload(
        uow, owner, owner.root_folder_id, "shared.bin", stream(PAYLOAD), now=NOW
    )
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=node.id,
            subject=other.subject,
            role=Role.EDITOR,
            granted_by=owner.subject,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await svc.delete(uow, owner, node.id, now=NOW)

    with pytest.raises(NotFoundError):
        await svc.purge(uow, other, node.id, objects=store, now=LATER)

    assert await uow.nodes.get(node.id) is not None
    assert store.deleted == []


# --- destruction -----------------------------------------------------------


async def test_purging_a_file_deletes_its_object_and_row() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    node = await content(store).upload(
        uow, user, user.root_folder_id, "gone.bin", stream(PAYLOAD), now=NOW
    )
    keys = [v.object_key for v in await uow.versions.list_for_node(node.id)]
    await svc.delete(uow, user, node.id, now=NOW)

    result = await svc.purge(uow, user, node.id, objects=store, now=LATER)

    assert result.nodes_deleted == 1
    assert result.objects_deleted == len(keys)
    assert result.bytes_reclaimed == len(PAYLOAD)
    assert await uow.nodes.get(node.id) is None
    for key in keys:
        assert key not in store.objects
    assert await uow.versions.list_for_node(node.id) == ()


async def test_purging_a_folder_deletes_every_descendant_object() -> None:
    """Regression: the DB cascade would strand descendants' objects.

    `NodeRow.parent_id` cascades, so removing the folder's row alone deletes
    the descendant rows and leaves their objects in the store with nothing
    referencing them.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    files = content(store)

    outer = await svc.create_folder(uow, user, user.root_folder_id, "outer", now=NOW)
    inner = await svc.create_folder(uow, user, outer.node.id, "inner", now=NOW)
    a = await files.upload(uow, user, outer.node.id, "a.bin", stream(PAYLOAD), now=NOW)
    b = await files.upload(uow, user, inner.node.id, "b.bin", stream(PAYLOAD), now=NOW)
    keys = [v.object_key for n in (a, b) for v in await uow.versions.list_for_node(n.id)]
    assert len(keys) == 2

    await svc.delete(uow, user, outer.node.id, now=NOW)
    result = await svc.purge(uow, user, outer.node.id, objects=store, now=LATER)

    assert result.nodes_deleted == 4  # outer, inner, a, b
    assert result.objects_deleted == 2
    for key in keys:
        assert key not in store.objects, "a descendant's object was left behind"
    assert await uow.nodes.get(outer.node.id) is None
    assert await uow.nodes.get(inner.node.id) is None
    assert await uow.nodes.get(a.id) is None
    assert await uow.nodes.get(b.id) is None


async def test_purging_drops_grants_and_wrapped_keys() -> None:
    uow = FakeUnitOfWork()
    owner = await provision(uow, "alice")
    other = await provision(uow, "bob")
    store = FakeObjectStore()
    svc = nodes()
    node = await content(store).upload(
        uow, owner, owner.root_folder_id, "was-shared.bin", stream(PAYLOAD), now=NOW
    )
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=node.id,
            subject=other.subject,
            role=Role.VIEWER,
            granted_by=owner.subject,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await svc.delete(uow, owner, node.id, now=NOW)

    await svc.purge(uow, owner, node.id, objects=store, now=LATER)

    assert await uow.grants.list_for_node(node.id) == ()


# --- quota -----------------------------------------------------------------


async def test_the_owners_quota_is_released_immediately() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    svc = nodes()
    node = await content(store).upload(
        uow, user, user.root_folder_id, "space.bin", stream(PAYLOAD), now=NOW
    )

    await svc.delete(uow, user, node.id, now=NOW)
    trashed = await uow.quotas.get(user.id)
    assert trashed is not None
    assert trashed.trashed_bytes == len(PAYLOAD)

    await svc.purge(uow, user, node.id, objects=store, now=LATER)

    after = await uow.quotas.get(user.id)
    assert after is not None
    assert after.trashed_bytes == 0
    assert after.live_bytes == 0


async def test_an_admin_purge_charges_the_owner_not_the_admin() -> None:
    uow = FakeUnitOfWork()
    owner = await provision(uow, "alice")
    admin = await provision(uow, "root")
    admin.is_admin = True
    store = FakeObjectStore()
    svc = nodes()
    node = await content(store).upload(
        uow, owner, owner.root_folder_id, "theirs.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, owner, node.id, now=NOW)

    result = await svc.purge(uow, admin, node.id, objects=store, now=LATER)

    assert result.bytes_reclaimed == len(PAYLOAD)
    owner_usage = await uow.quotas.get(owner.id)
    admin_usage = await uow.quotas.get(admin.id)
    assert owner_usage is not None and owner_usage.trashed_bytes == 0
    # The administrator's own accounting is untouched.
    assert admin_usage is not None
    assert admin_usage.trashed_bytes == 0
    assert admin_usage.live_bytes == 0


# --- auditing --------------------------------------------------------------


async def test_the_purge_is_recorded_with_the_owner_named() -> None:
    uow = FakeUnitOfWork()
    owner = await provision(uow, "alice")
    admin = await provision(uow, "root")
    admin.is_admin = True
    store = FakeObjectStore()
    svc = nodes()
    node = await content(store).upload(
        uow, owner, owner.root_folder_id, "audited.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(uow, owner, node.id, now=NOW)

    await svc.purge(uow, admin, node.id, objects=store, now=LATER)

    records = [r for r in uow.audit.records if r.action is AuditAction.NODE_PURGED]
    assert len(records) == 1
    record = records[0]
    assert record.actor_subject == admin.subject
    assert record.target_id == str(node.id)
    assert record.context["owner_id"] == str(owner.id)
    assert record.context["bytes"] == len(PAYLOAD)
    # A cross-user record never carries the file name.
    assert "name" not in record.context


# --- the retention sweep still behaves ------------------------------------


async def test_the_retention_job_and_the_endpoint_agree() -> None:
    """Both paths go through `purge_one`, so they must reclaim the same way."""
    by_job = FakeUnitOfWork()
    job_user = await provision(by_job)
    job_store = FakeObjectStore()
    job_node = await content(job_store).upload(
        by_job, job_user, job_user.root_folder_id, "x.bin", stream(PAYLOAD), now=NOW
    )
    await nodes().delete(by_job, job_user, job_node.id, now=NOW)
    job_result = await PurgeJob(job_store, make_settings(trash_retention_days=30)).run(
        by_job, now=NOW + timedelta(days=40)
    )

    on_demand = FakeUnitOfWork()
    user = await provision(on_demand)
    store = FakeObjectStore()
    svc = nodes()
    node = await content(store).upload(
        on_demand, user, user.root_folder_id, "x.bin", stream(PAYLOAD), now=NOW
    )
    await svc.delete(on_demand, user, node.id, now=NOW)
    direct = await svc.purge(on_demand, user, node.id, objects=store, now=LATER)

    assert job_result.bytes_reclaimed == direct.bytes_reclaimed
    assert job_result.objects_deleted == direct.objects_deleted
    assert job_result.nodes_purged == direct.nodes_deleted
