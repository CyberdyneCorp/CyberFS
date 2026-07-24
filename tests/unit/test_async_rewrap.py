"""Async DEK rewrap for large shared subtrees -- `sharing/spec.md`.

A grant over a subtree with more than `ASYNC_REWRAP_THRESHOLD_NODES` encrypted
nodes is created *pending*: its rewrap is handed to the background worker and the
grant confers no access until every encrypted descendant is rewrapped for the
recipient. A partially rewrapped share never appears usable. Below the threshold
sharing stays synchronous and immediately usable -- unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.adapters.outbound.cipher import AesGcmContentCipher
from cyberfs.adapters.outbound.crypto import KEY_BYTES, MasterKeyProvider
from cyberfs.application.content import ContentService
from cyberfs.application.encryption import EncryptionService
from cyberfs.application.jobs import RewrapJob
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.sharing import SharingService
from cyberfs.domain.audit import AuditAction
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import NotFoundError, PermissionDeniedError
from cyberfs.domain.nodes import Node
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.sharing import Role
from cyberfs.domain.users import User

from .conftest import make_settings
from .fakes import FakeObjectStore, FakeUnitOfWork, stream
from .test_sharing_service import FakeDirectory

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3
FRAME = 1024
MASTER = b"\x11" * KEY_BYTES
PAYLOAD = b"confidential figures\n" * 40


def provider() -> MasterKeyProvider:
    return MasterKeyProvider(MASTER)


def encryption(p: MasterKeyProvider) -> EncryptionService:
    return EncryptionService(p, AesGcmContentCipher(FRAME))


def files(store: FakeObjectStore, enc: EncryptionService) -> ContentService:
    return ContentService(
        store,
        max_upload_bytes=10 * GB,
        upload_chunk_bytes=64,
        version_retention_count=10,
        encryption=enc,
    )


def nodes() -> NodeService:
    return NodeService(max_tree_depth=64, page_size_max=100)


async def provision(uow: FakeUnitOfWork, p: MasterKeyProvider, subject: str) -> User:
    return await ProvisioningService(p, default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


class SpyCache:
    """Records which subjects had their permission decisions dropped."""

    def __init__(self) -> None:
        self.invalidated: list[str] = []

    async def invalidate_permissions_for_subject(self, subject: str) -> None:
        self.invalidated.append(subject)


class FlakyRewrapper:
    """Wraps a real rewrapper but fails after N successful rewraps.

    Simulates a crash partway through the worker's subtree walk.
    """

    def __init__(self, inner: EncryptionService, *, fail_after: int) -> None:
        self._inner = inner
        self._fail_after = fail_after
        self.calls = 0

    async def rewrap_for(self, uow: UnitOfWork, node: Node, subject: str, now: datetime) -> None:
        self.calls += 1
        if self.calls > self._fail_after:
            raise RuntimeError("worker crashed mid-rewrap")
        await self._inner.rewrap_for(uow, node, subject, now)

    async def revoke_for(self, uow: UnitOfWork, node: Node, subject: str) -> None:
        await self._inner.revoke_for(uow, node, subject)


class World:
    """Alice owns a folder holding encrypted files; Bob is a would-be recipient."""

    def __init__(self) -> None:
        self.uow = FakeUnitOfWork()
        self.provider = provider()
        self.enc = encryption(self.provider)
        self.store = FakeObjectStore()
        self.content = files(self.store, self.enc)

    async def setup(self, *, threshold: int, encrypted_files: int) -> World:
        self.alice = await provision(self.uow, self.provider, "alice")
        self.bob = await provision(self.uow, self.provider, "bob")
        self.sharing = SharingService(
            FakeDirectory(),
            keys=self.enc,
            async_rewrap_threshold_nodes=threshold,
        )
        folder = await nodes().create_folder(
            self.uow, self.alice, self.alice.root_folder_id, "team", now=NOW
        )
        self.folder = folder.node
        self.uploaded: list[Node] = []
        for index in range(encrypted_files):
            node = await self.content.upload(
                self.uow,
                self.alice,
                self.folder.id,
                f"secret-{index}.txt",
                stream(PAYLOAD),
                encrypted=True,
                now=NOW,
            )
            self.uploaded.append(node)
        return self

    async def bob_role_on(self, node: Node) -> Role | None:
        return await nodes().effective_role(self.uow, "bob", self.bob.id, node)

    def rewrap_job(self, *, keys: object | None = None, cache: SpyCache | None = None) -> RewrapJob:
        return RewrapJob(keys or self.enc, cache, make_settings())  # type: ignore[arg-type]


async def make_world(*, threshold: int, encrypted_files: int) -> World:
    return await World().setup(threshold=threshold, encrypted_files=encrypted_files)


# --- below-threshold: unchanged, synchronous, immediately usable -----------


async def test_below_threshold_share_is_synchronous_and_usable() -> None:
    world = await make_world(threshold=100, encrypted_files=2)

    grant = await world.sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)

    assert grant.pending is False
    # Access is immediate: no pending state, the recipient reads at once.
    assert await world.bob_role_on(world.uploaded[0]) is Role.VIEWER
    plan = await world.content.download(world.uow, world.bob, world.uploaded[0].id)
    assert await _collect(plan.stream) == PAYLOAD
    roots = await world.sharing.shared_with_me(world.uow, world.bob)
    assert world.folder.id in {n.id for n in roots}


# --- above-threshold: pending, confers no access ---------------------------


async def test_above_threshold_share_is_pending_and_confers_no_access() -> None:
    world = await make_world(threshold=1, encrypted_files=2)

    grant = await world.sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)

    assert grant.pending is True
    # Invariant A: a pending grant grants nothing, anywhere in the subtree.
    assert await world.bob_role_on(world.folder) is None
    assert await world.bob_role_on(world.uploaded[0]) is None
    with pytest.raises((PermissionDeniedError, NotFoundError)):
        await nodes().get(world.uow, world.bob, world.folder.id)
    with pytest.raises((PermissionDeniedError, NotFoundError)):
        await world.content.download(world.uow, world.bob, world.uploaded[0].id)
    # It does not appear in the recipient's shared view.
    assert await world.sharing.shared_with_me(world.uow, world.bob) == ()
    # But the grant creation is still audited.
    assert any(r.action is AuditAction.GRANT_CREATED for r in world.uow.audit.records)


async def test_owner_sees_a_pending_grant_in_list_grants() -> None:
    world = await make_world(threshold=1, encrypted_files=2)
    await world.sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)

    listed = await world.sharing.list_grants(world.uow, world.alice, world.folder.id)

    assert len(listed) == 1
    assert listed[0].subject == "bob"
    assert listed[0].pending is True


# --- the rewrap worker completes it ----------------------------------------


async def test_rewrap_job_activates_a_pending_grant() -> None:
    world = await make_world(threshold=1, encrypted_files=2)
    await world.sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)
    cache = SpyCache()

    result = await world.rewrap_job(cache=cache).run(world.uow, now=LATER)

    assert result.grants_activated == 1
    assert result.nodes_rewrapped == 2
    # Invariant D: on activation every encrypted descendant is decryptable.
    assert await world.bob_role_on(world.uploaded[0]) is Role.VIEWER
    for node in world.uploaded:
        plan = await world.content.download(world.uow, world.bob, node.id)
        assert await _collect(plan.stream) == PAYLOAD
    # The recipient's stale "no access" decisions were dropped.
    assert cache.invalidated == ["bob"]
    # The grant is now active and visible in the shared view.
    assert await world.sharing.shared_with_me(world.uow, world.bob) != ()


async def test_rewrap_job_is_idempotent_on_rerun() -> None:
    world = await make_world(threshold=1, encrypted_files=2)
    await world.sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)

    await world.rewrap_job().run(world.uow, now=LATER)
    keys_after_first = dict(world.uow.keys.data_keys)
    second = await world.rewrap_job().run(world.uow, now=LATER)

    # Nothing left pending, and no key was wrapped twice.
    assert second.grants_scanned == 0
    assert world.uow.keys.data_keys == keys_after_first
    assert await world.bob_role_on(world.uploaded[0]) is Role.VIEWER


async def test_crash_before_commit_leaves_grant_pending_then_a_clean_run_completes() -> None:
    world = await make_world(threshold=1, encrypted_files=2)
    await world.sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)
    flaky = FlakyRewrapper(world.enc, fail_after=1)

    with pytest.raises(RuntimeError):
        await world.rewrap_job(keys=flaky).run(world.uow, now=LATER)

    # Invariant C: the grant stays pending -- no half-usable access.
    still_pending = await world.uow.grants.list_pending(limit=10)
    assert len(still_pending) == 1
    assert await world.bob_role_on(world.uploaded[0]) is None

    # A subsequent clean run finishes it; the redo is safe.
    await world.rewrap_job().run(world.uow, now=LATER)
    assert await world.uow.grants.list_pending(limit=10) == ()
    assert await world.bob_role_on(world.uploaded[0]) is Role.VIEWER


async def test_create_during_pending_is_rewrapped_on_activation() -> None:
    """The gap closure: a file added while pending is rewrapped before access."""
    world = await make_world(threshold=1, encrypted_files=2)
    await world.sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)

    # Uploaded into the shared folder *while the grant is still pending*.
    late = await world.content.upload(
        world.uow,
        world.alice,
        world.folder.id,
        "late.txt",
        stream(PAYLOAD),
        encrypted=True,
        now=LATER,
    )

    result = await world.rewrap_job().run(world.uow, now=LATER)

    assert result.nodes_rewrapped == 3
    plan = await world.content.download(world.uow, world.bob, late.id)
    assert await _collect(plan.stream) == PAYLOAD


async def test_rewrap_job_drops_a_grant_whose_node_vanished() -> None:
    world = await make_world(threshold=1, encrypted_files=2)
    grant = await world.sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)
    await world.uow.nodes.delete_permanently(world.folder.id)

    result = await world.rewrap_job().run(world.uow, now=LATER)

    assert result.grants_activated == 0
    assert await world.uow.grants.get(grant.id) is None
    assert await world.uow.grants.list_pending(limit=10) == ()


# --- revoke and regrant edge cases -----------------------------------------


async def test_revoke_of_a_pending_grant_removes_it_cleanly() -> None:
    world = await make_world(threshold=1, encrypted_files=2)
    grant = await world.sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)
    # Simulate a partial rewrap already having landed one key.
    await world.enc.rewrap_for(world.uow, world.uploaded[0], "bob", NOW)

    await world.sharing.revoke(world.uow, world.alice, world.folder.id, "bob", now=LATER)

    assert await world.uow.grants.get(grant.id) is None
    assert await world.uow.keys.get_data_key(world.uploaded[0].id, "bob") is None
    assert await world.bob_role_on(world.folder) is None


async def test_regrant_of_an_active_grant_never_becomes_pending() -> None:
    world = await make_world(threshold=1, encrypted_files=2)
    # First share is small enough to be synchronous (raise the threshold via a
    # fresh service) so it lands active.
    active_sharing = SharingService(
        FakeDirectory(), keys=world.enc, async_rewrap_threshold_nodes=100
    )
    await active_sharing.grant(world.uow, world.alice, world.folder.id, "bob", Role.VIEWER)

    # A role change on the low-threshold service must not drop it to pending.
    regranted = await world.sharing.grant(
        world.uow, world.alice, world.folder.id, "bob", Role.EDITOR, now=LATER
    )

    assert regranted.pending is False
    assert await world.bob_role_on(world.uploaded[0]) is Role.EDITOR


async def _collect(source: object) -> bytes:
    buffer = bytearray()
    async for piece in source:  # type: ignore[attr-defined]
        buffer.extend(piece)
    return bytes(buffer)
