"""Upload, download, versions, quota -- `file-storage/spec.md`."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.content import ContentService, parse_range
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import (
    IntegrityFailureError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    QuotaExceededError,
    ValidationError,
)
from cyberfs.domain.nodes import Node, NodeKind
from cyberfs.domain.sharing import Grant, Role
from cyberfs.domain.users import User

from .fakes import FakeKeyProvider, FakeObjectStore, FakeUnitOfWork, stream

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3
PAYLOAD = b"the quick brown fox jumps over the lazy dog" * 4


async def provision(uow: FakeUnitOfWork, subject: str = "alice") -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


def content(store: FakeObjectStore, **kw: object) -> ContentService:
    return ContentService(
        store,
        max_upload_bytes=kw.pop("max_upload_bytes", 10 * GB),  # type: ignore[arg-type]
        upload_chunk_bytes=8,
        version_retention_count=kw.pop("version_retention_count", 10),  # type: ignore[arg-type]
    )


async def collect(plan_stream: object) -> bytes:
    buffer = bytearray()
    async for chunk in plan_stream:  # type: ignore[attr-defined]
        buffer.extend(chunk)
    return bytes(buffer)


@pytest.fixture
async def world() -> tuple[FakeUnitOfWork, User, FakeObjectStore, ContentService]:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    store = FakeObjectStore()
    return uow, user, store, content(store)


World = tuple[FakeUnitOfWork, User, FakeObjectStore, ContentService]


# --- range parsing ---------------------------------------------------------


def test_no_range_header_serves_the_whole_object() -> None:
    assert parse_range(None, 100) is None


def test_simple_range() -> None:
    span = parse_range("bytes=10-19", 100)
    assert span is not None
    assert (span.start, span.length, span.end) == (10, 10, 19)


def test_open_ended_range_runs_to_the_end() -> None:
    span = parse_range("bytes=90-", 100)
    assert span is not None
    assert (span.start, span.length) == (90, 10)


def test_suffix_range_takes_the_last_bytes() -> None:
    span = parse_range("bytes=-10", 100)
    assert span is not None
    assert (span.start, span.length) == (90, 10)


def test_suffix_larger_than_the_object_is_clamped() -> None:
    span = parse_range("bytes=-500", 100)
    assert span is not None
    assert (span.start, span.length) == (0, 100)


def test_range_end_past_the_object_is_clamped() -> None:
    span = parse_range("bytes=50-999", 100)
    assert span is not None
    assert span.length == 50


@pytest.mark.parametrize(
    "header",
    ["bytes=200-300", "bytes=50-10", "items=0-10", "bytes=abc-def", "garbage", "bytes=-"],
)
def test_unsatisfiable_or_malformed_ranges_serve_the_whole_object(header: str) -> None:
    assert parse_range(header, 100) is None


def test_multi_range_is_declined() -> None:
    """A legal response; multipart/byteranges is not worth the complexity."""
    assert parse_range("bytes=0-10,20-30", 100) is None


def test_range_on_an_empty_object_is_none() -> None:
    assert parse_range("bytes=0-10", 0) is None


# --- upload ----------------------------------------------------------------


async def test_upload_creates_a_file(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "notes.txt", stream(PAYLOAD), now=NOW)

    assert node.is_file
    assert node.size_bytes == len(PAYLOAD)
    assert node.current_version_id is not None


async def test_uploaded_bytes_reach_the_store(world: World) -> None:
    uow, user, store, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "notes.txt", stream(PAYLOAD), now=NOW)
    key = f"{node.owner_id}/{node.id}/{node.current_version_id}"
    assert store.objects[key] == PAYLOAD


async def test_the_plaintext_digest_is_recorded(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "notes.txt", stream(PAYLOAD), now=NOW)
    version = await uow.versions.get(node.current_version_id)  # type: ignore[arg-type]
    assert version is not None
    assert version.plaintext_digest == hashlib.sha256(PAYLOAD).hexdigest()


async def test_an_empty_upload_is_allowed(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "empty.txt", stream(b""), now=NOW)
    assert node.size_bytes == 0


async def test_content_type_is_recorded(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(
        uow,
        user,
        user.root_folder_id,
        "a.json",
        stream(b"{}"),
        content_type="application/json",
        now=NOW,
    )
    assert node.content_type == "application/json"


async def test_upload_into_a_file_is_refused(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"x"), now=NOW)
    with pytest.raises(ValidationError, match="folder"):
        await svc.upload(uow, user, node.id, "b.txt", stream(b"y"), now=NOW)


async def test_upload_over_a_folder_name_is_refused(world: World) -> None:
    uow, user, _, svc = world
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    await nodes.create_folder(uow, user, user.root_folder_id, "reports", now=NOW)

    with pytest.raises(ValidationError, match="folder already uses"):
        await svc.upload(uow, user, user.root_folder_id, "reports", stream(b"x"), now=NOW)


async def test_a_viewer_cannot_upload() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = content(FakeObjectStore())
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    folder = await nodes.create_folder(uow, alice, alice.root_folder_id, "team", now=NOW)
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=folder.node.id,
            subject="bob",
            role=Role.VIEWER,
            granted_by="alice",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    with pytest.raises(PermissionDeniedError):
        await svc.upload(uow, bob, folder.node.id, "sneaky.txt", stream(b"x"), now=NOW)


# --- upload limits ---------------------------------------------------------


async def test_a_declared_oversize_upload_is_refused_before_storing(world: World) -> None:
    uow, user, store, _ = world
    svc = content(store, max_upload_bytes=10)

    with pytest.raises(PayloadTooLargeError):
        await svc.upload(
            uow,
            user,
            user.root_folder_id,
            "big.bin",
            stream(b"x" * 100),
            declared_length=100,
            now=NOW,
        )
    assert store.objects == {}, "nothing should have been written"


async def test_an_undeclared_oversize_upload_is_refused_mid_stream(world: World) -> None:
    """A client that omits Content-Length cannot smuggle a huge body."""
    uow, user, store, _ = world
    svc = content(store, max_upload_bytes=16)

    with pytest.raises(PayloadTooLargeError):
        await svc.upload(uow, user, user.root_folder_id, "big.bin", stream(b"x" * 100), now=NOW)


async def test_a_length_mismatch_is_rejected_and_the_object_removed(world: World) -> None:
    uow, user, store, svc = world

    with pytest.raises(ValidationError, match="Content-Length"):
        await svc.upload(
            uow,
            user,
            user.root_folder_id,
            "short.bin",
            stream(b"only-ten-b"),
            declared_length=999,
            now=NOW,
        )
    assert store.objects == {}, "the mismatched object must not linger"


async def test_a_failed_upload_leaves_no_visible_file(world: World) -> None:
    uow, user, store, _ = world
    svc = content(store, max_upload_bytes=8)

    with pytest.raises(PayloadTooLargeError):
        await svc.upload(uow, user, user.root_folder_id, "big.bin", stream(b"x" * 100), now=NOW)

    page = await uow.nodes.list_children(user.root_folder_id, limit=10)
    assert all(n.current_version_id is None for n in page.items)


# --- quota -----------------------------------------------------------------


async def test_upload_is_charged_to_the_owner(world: World) -> None:
    uow, user, _, svc = world
    await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)

    usage = await uow.quotas.get(user.id)
    assert usage is not None
    assert usage.live_bytes == len(PAYLOAD)


async def test_upload_beyond_the_quota_is_refused(world: World) -> None:
    uow, user, _, svc = world
    user.quota_bytes = 10

    with pytest.raises(QuotaExceededError):
        await svc.upload(
            uow,
            user,
            user.root_folder_id,
            "big.bin",
            stream(PAYLOAD),
            declared_length=len(PAYLOAD),
            now=NOW,
        )


async def test_a_recipient_is_not_charged_for_someone_elses_file() -> None:
    """`file-storage/spec.md`: bytes count only against the owner."""
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = content(FakeObjectStore())
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    folder = await nodes.create_folder(uow, alice, alice.root_folder_id, "team", now=NOW)
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=folder.node.id,
            subject="bob",
            role=Role.EDITOR,
            granted_by="alice",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    await svc.upload(uow, bob, folder.node.id, "bobs-upload.bin", stream(PAYLOAD), now=NOW)

    alice_usage = await uow.quotas.get(alice.id)
    bob_usage = await uow.quotas.get(bob.id)
    assert alice_usage is not None and alice_usage.live_bytes == len(PAYLOAD)
    assert bob_usage is not None and bob_usage.live_bytes == 0


# --- versions --------------------------------------------------------------


async def test_replacing_content_creates_a_new_version(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"one"), now=NOW)
    first = node.current_version_id

    node = await svc.replace(uow, user, node.id, stream(b"two!"), now=LATER)

    assert node.current_version_id != first
    assert len(await uow.versions.list_for_node(node.id)) == 2


async def test_uploading_the_same_name_adds_a_version(world: World) -> None:
    uow, user, _, svc = world
    first = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"one"), now=NOW)
    second = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"two"), now=LATER)

    assert first.id == second.id
    assert len(await uow.versions.list_for_node(first.id)) == 2


async def test_the_old_version_moves_from_live_to_version_bytes(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"x" * 10), now=NOW)
    await svc.replace(uow, user, node.id, stream(b"y" * 20), now=LATER)

    usage = await uow.quotas.get(user.id)
    assert usage is not None
    assert usage.live_bytes == 20
    assert usage.version_bytes == 10


async def test_versions_beyond_retention_are_pruned(world: World) -> None:
    uow, user, store, _ = world
    svc = content(store, version_retention_count=2)
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"v1"), now=NOW)
    for i in range(2, 6):
        await svc.replace(uow, user, node.id, stream(f"v{i}".encode()), now=LATER)

    assert len(await uow.versions.list_for_node(node.id)) == 2


async def test_pruning_deletes_the_objects_it_drops(world: World) -> None:
    uow, user, store, _ = world
    svc = content(store, version_retention_count=1)
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"v1"), now=NOW)
    await svc.replace(uow, user, node.id, stream(b"v2"), now=LATER)

    assert len(store.deleted) == 1, "the dropped version's bytes must be released"


async def test_a_metadata_change_creates_no_version(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"x"), now=NOW)
    nodes = NodeService(max_tree_depth=64, page_size_max=100)

    await nodes.rename(uow, user, node.id, "b.txt", now=LATER)

    assert len(await uow.versions.list_for_node(node.id)) == 1


async def test_restoring_a_version_adds_a_new_one(world: World) -> None:
    """History is never deleted by a restore."""
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"first"), now=NOW)
    original = node.current_version_id
    await svc.replace(uow, user, node.id, stream(b"second"), now=LATER)

    restored = await svc.restore_version(uow, user, node.id, original, now=LATER)  # type: ignore[arg-type]

    assert restored.current_version_id not in (original, None)
    assert len(await uow.versions.list_for_node(node.id)) == 3


async def test_a_restored_version_carries_the_sealing_id_that_opens_its_bytes(
    world: World,
) -> None:
    """A restore copies ciphertext, so it must copy what authenticates it.

    `seal` binds the version id into the AEAD; a restore writes a new row for the
    *source's* bytes, so taking the new row's own id as the associated data
    authenticated against an id those bytes were never sealed under, and the
    download decrypted to nothing. Chosen in the application layer, so the fake
    is enough to pin it.
    """
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"first"), now=NOW)
    original_id = node.current_version_id
    await svc.replace(uow, user, node.id, stream(b"second"), now=LATER)

    restored = await svc.restore_version(uow, user, node.id, original_id, now=LATER)  # type: ignore[arg-type]

    versions = {v.id: v for v in await uow.versions.list_for_node(node.id)}
    source = versions[original_id]  # type: ignore[index]
    new = versions[restored.current_version_id]  # type: ignore[index]
    assert new.id != source.id, "a restore adds a version rather than reusing one"
    assert new.seal_version_id == source.seal_version_id


async def test_restoring_a_restored_version_keeps_the_original_sealing_id(
    world: World,
) -> None:
    """The transitive case: a copy of a copy still opens.

    This is why the source's `seal_version_id` is carried rather than its `id` --
    the latter would bind the second copy to a row whose bytes it does not hold,
    and no chain-walk at read time would be needed to notice.
    """
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"first"), now=NOW)
    first_id = node.current_version_id
    await svc.replace(uow, user, node.id, stream(b"second"), now=LATER)
    once = await svc.restore_version(uow, user, node.id, first_id, now=LATER)  # type: ignore[arg-type]

    twice = await svc.restore_version(uow, user, node.id, once.current_version_id, now=LATER)  # type: ignore[arg-type]

    versions = {v.id: v for v in await uow.versions.list_for_node(node.id)}
    original = versions[first_id]  # type: ignore[index]
    assert versions[twice.current_version_id].seal_version_id == original.seal_version_id  # type: ignore[index]


async def test_a_restored_version_has_the_old_content(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"first"), now=NOW)
    original = node.current_version_id
    await svc.replace(uow, user, node.id, stream(b"second"), now=LATER)
    await svc.restore_version(uow, user, node.id, original, now=LATER)  # type: ignore[arg-type]

    plan = await svc.download(uow, user, node.id)
    assert await collect(plan.stream) == b"first"


# --- download --------------------------------------------------------------


async def test_download_returns_the_content(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)

    plan = await svc.download(uow, user, node.id)
    assert await collect(plan.stream) == PAYLOAD
    assert plan.served_bytes == len(PAYLOAD)
    assert not plan.is_partial


async def test_download_of_a_range(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)

    plan = await svc.download(uow, user, node.id, range_header="bytes=5-14")
    assert await collect(plan.stream) == PAYLOAD[5:15]
    assert plan.is_partial
    assert plan.served_bytes == 10


async def test_download_of_a_specific_version(world: World) -> None:
    uow, user, _, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"first"), now=NOW)
    original = node.current_version_id
    await svc.replace(uow, user, node.id, stream(b"second"), now=LATER)

    plan = await svc.download(uow, user, node.id, version_id=original)
    assert await collect(plan.stream) == b"first"


async def test_download_without_permission_is_not_found() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = content(FakeObjectStore())
    node = await svc.upload(uow, alice, alice.root_folder_id, "private.bin", stream(b"x"), now=NOW)

    with pytest.raises(NotFoundError):
        await svc.download(uow, bob, node.id)


async def test_download_of_a_folder_is_refused(world: World) -> None:
    uow, user, _, svc = world
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    folder = await nodes.create_folder(uow, user, user.root_folder_id, "f", now=NOW)

    with pytest.raises(ValidationError, match="only a file"):
        await svc.download(uow, user, folder.node.id)


async def test_download_of_a_file_with_no_content_is_not_found(world: World) -> None:
    uow, user, _, svc = world
    node = Node(
        id=uuid.uuid4(),
        owner_id=user.id,
        kind=NodeKind.FILE,
        name="empty.txt",
        parent_id=user.root_folder_id,
        created_at=NOW,
        updated_at=NOW,
    )
    await uow.nodes.add(node)

    with pytest.raises(NotFoundError, match="no content"):
        await svc.download(uow, user, node.id)


async def test_a_tampered_object_fails_its_digest_check(world: World) -> None:
    """`file-storage/spec.md`: a digest mismatch aborts with integrity_failure."""
    uow, user, store, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)
    key = f"{node.owner_id}/{node.id}/{node.current_version_id}"
    store.objects[key] = b"tampered" + PAYLOAD[8:]

    plan = await svc.download(uow, user, node.id)
    with pytest.raises(IntegrityFailureError):
        await collect(plan.stream)


async def test_a_range_read_skips_the_whole_object_digest(world: World) -> None:
    """A partial read cannot be checked against a whole-object digest."""
    uow, user, store, svc = world
    node = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)
    key = f"{node.owner_id}/{node.id}/{node.current_version_id}"
    store.objects[key] = b"tampered" + PAYLOAD[8:]

    plan = await svc.download(uow, user, node.id, range_header="bytes=20-29")
    assert len(await collect(plan.stream)) == 10


# --- duplication -----------------------------------------------------------


async def test_duplicate_copies_the_current_version(world: World) -> None:
    uow, user, store, svc = world
    source = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)
    target = Node(
        id=uuid.uuid4(),
        owner_id=user.id,
        kind=source.kind,
        name="b.bin",
        parent_id=user.root_folder_id,
        created_at=NOW,
        updated_at=NOW,
    )

    await uow.nodes.add(target)
    copied = await svc.duplicate(uow, source, target, LATER)

    assert copied == len(PAYLOAD)
    assert target.current_version_id is not None
    key = f"{target.owner_id}/{target.id}/{target.current_version_id}"
    assert store.objects[key] == PAYLOAD


async def test_duplicating_a_file_with_no_content_copies_nothing(world: World) -> None:
    uow, user, _, svc = world
    empty = Node(
        id=uuid.uuid4(),
        owner_id=user.id,
        kind=NodeKind.FILE,
        name="a",
        parent_id=user.root_folder_id,
        created_at=NOW,
        updated_at=NOW,
    )
    assert await svc.duplicate(uow, empty, empty, LATER) == 0


# --- trash accounting (regression) -----------------------------------------


async def test_soft_delete_moves_bytes_from_live_to_trashed(world: World) -> None:
    """Without this the purge frees nothing and capacity leaks permanently.

    `file-storage/spec.md` requires total usage to be unchanged by a delete,
    but the live/trashed split is what purge and the admin view depend on.
    """
    uow, user, _, svc = world
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    node = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)

    await nodes.delete(uow, user, node.id, now=LATER)

    usage = await uow.quotas.get(user.id)
    assert usage is not None
    assert usage.live_bytes == 0
    assert usage.trashed_bytes == len(PAYLOAD)
    assert usage.total_bytes == len(PAYLOAD), "a delete must not change the total"


async def test_restore_moves_bytes_back_to_live(world: World) -> None:
    uow, user, _, svc = world
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    node = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)
    await nodes.delete(uow, user, node.id, now=LATER)

    await nodes.restore(uow, user, node.id, now=LATER)

    usage = await uow.quotas.get(user.id)
    assert usage is not None
    assert usage.live_bytes == len(PAYLOAD)
    assert usage.trashed_bytes == 0


async def test_deleting_a_folder_trashes_its_descendants_bytes(world: World) -> None:
    uow, user, _, svc = world
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    folder = await nodes.create_folder(uow, user, user.root_folder_id, "docs", now=NOW)
    await svc.upload(uow, user, folder.node.id, "inner.bin", stream(PAYLOAD), now=NOW)

    await nodes.delete(uow, user, folder.node.id, now=LATER)

    usage = await uow.quotas.get(user.id)
    assert usage is not None
    assert usage.trashed_bytes == len(PAYLOAD)


async def test_a_copied_file_is_downloadable(world: World) -> None:
    """Regression: a copy used to get bytes but no version row, so the
    duplicate existed in the tree and 404'd on download."""
    uow, user, _, svc = world
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    source = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)
    target = await nodes.create_folder(uow, user, user.root_folder_id, "target", now=NOW)

    view = await nodes.copy(uow, user, source.id, target.node.id, content=svc, now=LATER)

    assert view.node.current_version_id is not None
    plan = await svc.download(uow, user, view.node.id)
    assert await collect(plan.stream) == PAYLOAD


async def test_a_copy_has_its_own_version_history(world: World) -> None:
    uow, user, _, svc = world
    nodes = NodeService(max_tree_depth=64, page_size_max=100)
    source = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(b"v1"), now=NOW)
    await svc.replace(uow, user, source.id, stream(b"v2"), now=LATER)
    target = await nodes.create_folder(uow, user, user.root_folder_id, "target", now=NOW)

    view = await nodes.copy(uow, user, source.id, target.node.id, content=svc, now=LATER)

    copied_versions = await uow.versions.list_for_node(view.node.id)
    assert len(copied_versions) == 1, "a copy starts a fresh history"
