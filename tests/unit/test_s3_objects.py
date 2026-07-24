"""S3 object orchestration -- `s3-compatibility/spec.md`, read path (phase 6).

Exercises the application service against the in-memory unit of work: the own
tree and the reserved ``shared/<owner>/…`` view, the two refusals that keep
bucket names from becoming a directory, trash exclusion, plaintext sizes for
encrypted files, `Range`, and version-parameter irrelevance. Content itself is
delegated to `ContentService`, so a plaintext round trip through it proves the
delegation without re-testing decryption here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from cyberfs.adapters.outbound.cipher import AesGcmContentCipher
from cyberfs.adapters.outbound.crypto import KEY_BYTES, MasterKeyProvider
from cyberfs.application.content import ContentService
from cyberfs.application.encryption import EncryptionService
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.s3_objects import S3ObjectService
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import (
    NoSuchBucketError,
    NoSuchKeyError,
    PermissionDeniedError,
    QuotaExceededError,
    ValidationError,
)
from cyberfs.domain.nodes import EncryptionDefault, Node, NodeKind
from cyberfs.domain.s3.listing import ListRequest
from cyberfs.domain.s3.namespace import resolve_key
from cyberfs.domain.sharing import Grant, Role
from cyberfs.domain.users import User

from .fakes import FakeKeyProvider, FakeObjectStore, FakeUnitOfWork, stream

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
GB = 1024**3


async def provision(uow: FakeUnitOfWork, subject: str) -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


def build_content(store: FakeObjectStore) -> ContentService:
    return ContentService(
        store, max_upload_bytes=10 * GB, upload_chunk_bytes=8, version_retention_count=10
    )


def build_service(store: FakeObjectStore) -> S3ObjectService:
    return S3ObjectService(
        NodeService(max_tree_depth=64, page_size_max=1000),
        build_content(store),
        page_size_max=1000,
    )


async def collect(chunks: AsyncIterator[bytes]) -> bytes:
    buffer = bytearray()
    async for chunk in chunks:
        buffer.extend(chunk)
    return bytes(buffer)


class World:
    """Alice with a small tree, plus the services under test."""

    def __init__(
        self,
        uow: FakeUnitOfWork,
        alice: User,
        store: FakeObjectStore,
        service: S3ObjectService,
        content: ContentService,
        nodes: NodeService,
    ) -> None:
        self.uow = uow
        self.alice = alice
        self.store = store
        self.service = service
        self.content = content
        self.nodes = nodes

    async def folder(self, parent: uuid.UUID, name: str, user: User | None = None) -> uuid.UUID:
        view = await self.nodes.create_folder(self.uow, user or self.alice, parent, name, now=NOW)
        return view.node.id

    async def file(
        self, parent: uuid.UUID, name: str, body: bytes, user: User | None = None
    ) -> Node:
        return await self.content.upload(
            self.uow, user or self.alice, parent, name, stream(body), now=NOW
        )


@pytest.fixture
async def world() -> World:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    store = FakeObjectStore()
    nodes = NodeService(max_tree_depth=64, page_size_max=1000)
    content = build_content(store)
    service = S3ObjectService(nodes, content, page_size_max=1000)
    w = World(uow, alice, store, service, content, nodes)
    reports = await w.folder(alice.root_folder_id, "reports")
    await w.file(reports, "q3.xlsx", b"third quarter numbers")
    y2024 = await w.folder(reports, "2024")
    await w.file(y2024, "q1.xlsx", b"first quarter")
    await w.file(alice.root_folder_id, "notes.txt", b"top level note")
    return w


# --- buckets ---------------------------------------------------------------


def test_list_buckets_returns_exactly_the_callers_own(world: World) -> None:
    buckets = world.service.list_buckets(world.alice)
    assert len(buckets) == 1
    assert buckets[0].name == "alice"


def test_head_bucket_accepts_the_callers_own(world: World) -> None:
    world.service.head_bucket(world.alice, "alice")  # no raise


def test_head_bucket_refuses_a_foreign_bucket(world: World) -> None:
    with pytest.raises(NoSuchBucketError):
        world.service.head_bucket(world.alice, "bob")


async def test_listing_a_foreign_bucket_is_no_such_bucket(world: World) -> None:
    with pytest.raises(NoSuchBucketError):
        await world.service.list_objects(world.uow, world.alice, "bob", ListRequest())


# --- own-tree listing ------------------------------------------------------


async def test_delimiter_lists_direct_files_and_folder_prefixes(world: World) -> None:
    result = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(delimiter="/")
    )
    assert [e.key for e in result.entries] == ["notes.txt"]
    assert result.common_prefixes == ("reports/",)


async def test_prefix_descends_into_a_folder(world: World) -> None:
    result = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix="reports/", delimiter="/")
    )
    assert [e.key for e in result.entries] == ["reports/q3.xlsx"]
    assert result.common_prefixes == ("reports/2024/",)


async def test_recursive_listing_returns_every_descendant_file(world: World) -> None:
    result = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix="reports/")
    )
    assert {e.key for e in result.entries} == {"reports/q3.xlsx", "reports/2024/q1.xlsx"}


async def test_listed_size_is_the_stored_plaintext_size(world: World) -> None:
    result = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix="notes.txt")
    )
    assert result.entries[0].size == len(b"top level note")


# --- trash -----------------------------------------------------------------


async def test_a_trashed_file_is_absent_from_listings(world: World) -> None:
    reports = await world.uow.nodes.get_child_by_name(world.alice.root_folder_id, "reports")
    assert reports is not None
    q3 = await world.uow.nodes.get_child_by_name(reports.id, "q3.xlsx")
    assert q3 is not None
    await world.nodes.delete(world.uow, world.alice, q3.id, now=NOW)

    result = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix="reports/")
    )
    assert {e.key for e in result.entries} == {"reports/2024/q1.xlsx"}


async def test_a_trashed_file_is_not_gettable(world: World) -> None:
    note = await world.uow.nodes.get_child_by_name(world.alice.root_folder_id, "notes.txt")
    assert note is not None
    await world.nodes.delete(world.uow, world.alice, note.id, now=NOW)

    target = resolve_key("alice", "alice", "notes.txt")
    with pytest.raises(NoSuchKeyError):
        await world.service.get_object(world.uow, world.alice, target)


# --- encrypted plaintext size ----------------------------------------------


async def test_head_reports_plaintext_size_for_an_encrypted_file(world: World) -> None:
    node = Node(
        id=uuid.uuid4(),
        owner_id=world.alice.id,
        kind=NodeKind.FILE,
        name="secret.bin",
        parent_id=world.alice.root_folder_id,
        created_at=NOW,
        updated_at=NOW,
        size_bytes=4096,  # the recorded plaintext size
        content_type="application/octet-stream",
        encrypted=True,
    )
    await world.uow.nodes.add(node)

    info = await world.service.head_object(
        world.uow, world.alice, resolve_key("alice", "alice", "secret.bin")
    )
    assert info.size == 4096
    assert info.etag == node.etag


# --- get + range -----------------------------------------------------------


async def test_get_object_round_trips_bytes(world: World) -> None:
    plan = await world.service.get_object(
        world.uow, world.alice, resolve_key("alice", "alice", "reports/q3.xlsx")
    )
    assert await collect(plan.stream) == b"third quarter numbers"


async def test_get_object_honours_a_range(world: World) -> None:
    plan = await world.service.get_object(
        world.uow,
        world.alice,
        resolve_key("alice", "alice", "notes.txt"),
        range_header="bytes=0-2",
    )
    assert plan.is_partial
    assert plan.served_bytes == 3
    assert await collect(plan.stream) == b"top"


async def test_get_object_serves_the_current_version_ignoring_versions(world: World) -> None:
    # A second upload creates a new version; the S3 read never names a version,
    # so it always serves the current one.
    reports = await world.uow.nodes.get_child_by_name(world.alice.root_folder_id, "reports")
    assert reports is not None
    await world.file(reports.id, "q3.xlsx", b"revised numbers")

    plan = await world.service.get_object(
        world.uow, world.alice, resolve_key("alice", "alice", "reports/q3.xlsx")
    )
    assert await collect(plan.stream) == b"revised numbers"


async def test_a_missing_key_is_no_such_key(world: World) -> None:
    with pytest.raises(NoSuchKeyError):
        await world.service.get_object(
            world.uow, world.alice, resolve_key("alice", "alice", "reports/absent.xlsx")
        )


async def test_a_folder_shaped_key_is_no_such_key(world: World) -> None:
    with pytest.raises(NoSuchKeyError):
        await world.service.get_object(
            world.uow, world.alice, resolve_key("alice", "alice", "reports")
        )


# --- shared view -----------------------------------------------------------


async def _share_bob_projects(world: World) -> tuple[User, uuid.UUID]:
    """Give Bob a `projects/alpha.txt`, shared with Alice as viewer."""
    bob = await provision(world.uow, "bob")
    projects = await world.folder(bob.root_folder_id, "projects", user=bob)
    await world.file(projects, "alpha.txt", b"bob's alpha", user=bob)
    await world.file(bob.root_folder_id, "secret.txt", b"not shared", user=bob)
    await world.uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=projects,
            subject="alice",
            role=Role.VIEWER,
            granted_by="bob",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return bob, projects


async def test_shared_subtree_is_listable_under_the_reserved_prefix(world: World) -> None:
    bob, _ = await _share_bob_projects(world)

    result = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix=f"shared/{bob.subject}/projects/")
    )
    assert [e.key for e in result.entries] == [f"shared/{bob.subject}/projects/alpha.txt"]


async def test_the_shared_prefix_appears_at_the_bucket_root(world: World) -> None:
    await _share_bob_projects(world)

    result = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(delimiter="/")
    )
    assert "shared/" in result.common_prefixes
    assert "reports/" in result.common_prefixes


async def test_shared_owners_group_as_common_prefixes(world: World) -> None:
    bob, _ = await _share_bob_projects(world)

    result = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix="shared/", delimiter="/")
    )
    assert result.common_prefixes == (f"shared/{bob.subject}/",)


async def test_a_shared_file_is_gettable(world: World) -> None:
    bob, _ = await _share_bob_projects(world)

    plan = await world.service.get_object(
        world.uow,
        world.alice,
        resolve_key("alice", "alice", f"shared/{bob.subject}/projects/alpha.txt"),
    )
    assert await collect(plan.stream) == b"bob's alpha"


async def test_a_node_without_a_grant_is_absent_and_unreachable(world: World) -> None:
    bob, _ = await _share_bob_projects(world)

    listed = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix=f"shared/{bob.subject}/")
    )
    assert all("secret.txt" not in e.key for e in listed.entries)

    with pytest.raises(NoSuchKeyError):
        await world.service.get_object(
            world.uow,
            world.alice,
            resolve_key("alice", "alice", f"shared/{bob.subject}/secret.txt"),
        )


async def test_a_shared_root_that_is_a_file_is_listable_and_gettable(world: World) -> None:
    bob = await provision(world.uow, "bob")
    note = await world.file(bob.root_folder_id, "memo.txt", b"one file, shared", user=bob)
    await world.uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=note.id,
            subject="alice",
            role=Role.VIEWER,
            granted_by="bob",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    listed = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix="shared/")
    )
    assert f"shared/{bob.subject}/memo.txt" in {e.key for e in listed.entries}

    plan = await world.service.get_object(
        world.uow, world.alice, resolve_key("alice", "alice", f"shared/{bob.subject}/memo.txt")
    )
    assert await collect(plan.stream) == b"one file, shared"


async def test_addressing_an_owner_with_no_grant_is_no_such_key(world: World) -> None:
    await _share_bob_projects(world)  # alice holds a grant from bob, not charlie
    with pytest.raises(NoSuchKeyError):
        await world.service.get_object(
            world.uow, world.alice, resolve_key("alice", "alice", "shared/charlie/anything.txt")
        )


async def test_a_deleted_shared_root_is_skipped(world: World) -> None:
    bob, projects = await _share_bob_projects(world)
    await world.nodes.delete(world.uow, bob, projects, now=NOW)

    listed = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix="shared/")
    )
    assert listed.entries == ()
    assert listed.common_prefixes == ()


async def test_a_nested_grant_is_covered_by_the_grant_above_it(world: World) -> None:
    bob, projects = await _share_bob_projects(world)
    alpha = await world.uow.nodes.get_child_by_name(projects, "alpha.txt")
    assert alpha is not None
    # A second, redundant grant on a file already covered by the folder grant.
    await world.uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=alpha.id,
            subject="alice",
            role=Role.VIEWER,
            granted_by="bob",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    listed = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix=f"shared/{bob.subject}/projects/")
    )
    # The file is listed once, from the folder root -- not twice.
    assert [e.key for e in listed.entries] == [f"shared/{bob.subject}/projects/alpha.txt"]


async def test_a_prefix_that_descends_into_a_file_lists_nothing(world: World) -> None:
    result = await world.service.list_objects(
        world.uow, world.alice, "alice", ListRequest(prefix="notes.txt/deeper/")
    )
    assert result.entries == ()


# --- write path: PutObject -------------------------------------------------


def _target(key: str, *, subject: str = "alice", bucket: str = "alice") -> object:
    return resolve_key(subject, bucket, key)


async def test_put_object_stores_a_readable_file(world: World) -> None:
    await world.service.put_object(
        world.uow, world.alice, _target("uploads/new.txt"), stream(b"fresh bytes")
    )
    plan = await world.service.get_object(
        world.uow, world.alice, resolve_key("alice", "alice", "uploads/new.txt")
    )
    assert await collect(plan.stream) == b"fresh bytes"


async def test_put_object_creates_intermediate_folders(world: World) -> None:
    await world.service.put_object(
        world.uow, world.alice, _target("a/b/c/deep.txt"), stream(b"nested")
    )
    a = await world.uow.nodes.get_child_by_name(world.alice.root_folder_id, "a")
    assert a is not None and a.is_folder
    b = await world.uow.nodes.get_child_by_name(a.id, "b")
    assert b is not None and b.is_folder


async def test_put_object_over_an_existing_key_versions_it(world: World) -> None:
    reports = await world.uow.nodes.get_child_by_name(world.alice.root_folder_id, "reports")
    assert reports is not None
    node = await world.service.put_object(
        world.uow, world.alice, _target("reports/q3.xlsx"), stream(b"revised")
    )
    versions = await world.uow.versions.list_for_node(node.id)
    assert len(versions) == 2  # the original plus the S3 write
    plan = await world.service.get_object(
        world.uow, world.alice, resolve_key("alice", "alice", "reports/q3.xlsx")
    )
    assert await collect(plan.stream) == b"revised"


async def test_put_object_charges_the_owner_quota(world: World) -> None:
    before = await world.uow.quotas.get(world.alice.id)
    assert before is not None
    baseline = before.live_bytes
    await world.service.put_object(
        world.uow, world.alice, _target("charged.bin"), stream(b"x" * 100)
    )
    after = await world.uow.quotas.get(world.alice.id)
    assert after is not None
    assert after.live_bytes == baseline + 100


async def test_put_object_beyond_quota_is_refused_and_stores_nothing(world: World) -> None:
    world.alice.quota_bytes = 10
    stored_before = set(world.store.objects)
    with pytest.raises(QuotaExceededError):
        await world.service.put_object(
            world.uow,
            world.alice,
            _target("big.bin"),
            stream(b"y" * 100),
            declared_length=100,
        )
    # No object crossed into the store: the rejection stores neither content
    # (asserted here) nor -- via the transaction rollback the router commits
    # around -- metadata.
    assert set(world.store.objects) == stored_before


async def test_put_object_with_an_empty_key_is_refused(world: World) -> None:
    with pytest.raises(ValidationError):
        await world.service.put_object(world.uow, world.alice, _target(""), stream(b"nope"))


async def test_put_object_refuses_a_key_path_blocked_by_a_file(world: World) -> None:
    # `notes.txt` is a file at the root; it cannot also be a folder on a key path.
    with pytest.raises(ValidationError):
        await world.service.put_object(
            world.uow, world.alice, _target("notes.txt/child.txt"), stream(b"nope")
        )


# --- write path: encryption inheritance ------------------------------------


async def test_put_object_into_an_encrypted_folder_stores_ciphertext() -> None:
    uow = FakeUnitOfWork()
    alice = await ProvisioningService(
        MasterKeyProvider(b"\x01" * KEY_BYTES), default_quota_bytes=10 * GB
    ).resolve(uow, Principal(subject="alice"), now=NOW)
    store = FakeObjectStore()
    enc = EncryptionService(
        MasterKeyProvider(b"\x01" * KEY_BYTES),
        AesGcmContentCipher(1024),
        encryption_default_on=False,
    )
    content = ContentService(
        store,
        max_upload_bytes=10 * GB,
        upload_chunk_bytes=64,
        version_retention_count=10,
        encryption=enc,
    )
    nodes = NodeService(max_tree_depth=64, page_size_max=1000)
    service = S3ObjectService(nodes, content, page_size_max=1000)

    vault = await nodes.create_folder(
        uow, alice, alice.root_folder_id, "vault", encryption_default=EncryptionDefault.ON, now=NOW
    )
    secret = b"top secret contents that must never sit in plaintext"
    node = await service.put_object(
        uow, alice, resolve_key("alice", "alice", "vault/secret.txt"), stream(secret)
    )
    assert node.encrypted is True
    # The stored object is ciphertext; the plaintext appears nowhere in it.
    assert all(secret not in blob for blob in store.objects.values())
    # A read still yields the plaintext, proving the round trip.
    plan = await service.get_object(uow, alice, resolve_key("alice", "alice", "vault/secret.txt"))
    assert await collect(plan.stream) == secret
    assert vault.node.id  # the folder was the encryption source


# --- write path: DeleteObject ----------------------------------------------


async def test_delete_object_soft_deletes(world: World) -> None:
    await world.service.delete_object(
        world.uow, world.alice, resolve_key("alice", "alice", "notes.txt")
    )
    listed = await world.service.list_objects(world.uow, world.alice, "alice", ListRequest())
    assert all(e.key != "notes.txt" for e in listed.entries)
    # Soft delete: the row still exists, marked deleted, recoverable in trash.
    note = next(
        n
        for n in world.uow.nodes.by_id.values()
        if n.name == "notes.txt" and n.parent_id == world.alice.root_folder_id
    )
    assert note.is_deleted


async def test_delete_object_on_a_missing_key_is_idempotent(world: World) -> None:
    # No raise: S3 DeleteObject is idempotent.
    await world.service.delete_object(
        world.uow, world.alice, resolve_key("alice", "alice", "does/not/exist.txt")
    )


async def test_delete_objects_reports_each_key(world: World) -> None:
    outcomes = await world.service.delete_objects(
        world.uow, world.alice, "alice", ["notes.txt", "absent.txt"]
    )
    by_key = {o.key: o for o in outcomes}
    assert by_key["notes.txt"].deleted
    assert by_key["absent.txt"].deleted  # idempotent success


# --- write path: CopyObject ------------------------------------------------


async def test_copy_object_duplicates_content(world: World) -> None:
    node = await world.service.copy_object(
        world.uow,
        world.alice,
        resolve_key("alice", "alice", "notes.txt"),
        resolve_key("alice", "alice", "copies/note-copy.txt"),
    )
    assert node.owner_id == world.alice.id
    plan = await world.service.get_object(
        world.uow, world.alice, resolve_key("alice", "alice", "copies/note-copy.txt")
    )
    assert await collect(plan.stream) == b"top level note"


async def test_a_copy_carries_no_grants(world: World) -> None:
    bob, _ = await _share_bob_projects(world)
    # Alice copies Bob's shared file into her own tree.
    node = await world.service.copy_object(
        world.uow,
        world.alice,
        resolve_key("alice", "alice", f"shared/{bob.subject}/projects/alpha.txt"),
        resolve_key("alice", "alice", "mine/alpha.txt"),
    )
    grants = await world.uow.grants.list_for_subject("alice")
    assert all(g.node_id != node.id for g in grants)
    assert node.owner_id == world.alice.id


# --- write path: shared-prefix authorization -------------------------------


async def test_a_viewer_cannot_write_under_the_shared_prefix(world: World) -> None:
    bob, _ = await _share_bob_projects(world)  # alice holds only viewer
    with pytest.raises(PermissionDeniedError):
        await world.service.put_object(
            world.uow,
            world.alice,
            resolve_key("alice", "alice", f"shared/{bob.subject}/projects/intruder.txt"),
            stream(b"should be refused"),
        )


async def test_an_editor_can_write_under_the_shared_prefix(world: World) -> None:
    bob = await provision(world.uow, "bob")
    projects = await world.folder(bob.root_folder_id, "projects", user=bob)
    await world.uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=projects,
            subject="alice",
            role=Role.EDITOR,
            granted_by="bob",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    node = await world.service.put_object(
        world.uow,
        world.alice,
        resolve_key("alice", "alice", f"shared/{bob.subject}/projects/from-alice.txt"),
        stream(b"editor write"),
    )
    # The file lands in Bob's tree, charged to Bob, since he owns the folder.
    assert node.owner_id == bob.id
    plan = await world.service.get_object(
        world.uow,
        world.alice,
        resolve_key("alice", "alice", f"shared/{bob.subject}/projects/from-alice.txt"),
    )
    assert await collect(plan.stream) == b"editor write"
