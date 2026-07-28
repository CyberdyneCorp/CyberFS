"""Opt-in encryption, key hierarchy, rewrap, and rotation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.adapters.outbound.cipher import AesGcmContentCipher
from cyberfs.adapters.outbound.crypto import KEY_BYTES, MasterKeyProvider, master_key_id
from cyberfs.application.content import ContentService
from cyberfs.application.encryption import EncryptionService, resolve_encryption
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.sharing import SharingService
from cyberfs.domain.audit import AuditAction
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import KeyUnavailableError
from cyberfs.domain.nodes import EncryptionDefault, Node, NodeKind
from cyberfs.domain.sharing import Role
from cyberfs.domain.users import User

from .fakes import FakeObjectStore, FakeUnitOfWork, stream
from .test_sharing_service import FakeDirectory

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3
FRAME = 1024
MASTER_A = b"\x01" * KEY_BYTES
MASTER_B = b"\x02" * KEY_BYTES
PAYLOAD = b"confidential quarterly figures\n" * 60


def keys(master: bytes = MASTER_A, previous: bytes | None = None) -> MasterKeyProvider:
    return MasterKeyProvider(master, previous=previous)


def encryption(
    provider: MasterKeyProvider | None = None, *, default_on: bool = False
) -> EncryptionService:
    return EncryptionService(
        provider or keys(), AesGcmContentCipher(FRAME), encryption_default_on=default_on
    )


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


async def provision(uow: FakeUnitOfWork, provider: MasterKeyProvider, subject: str) -> User:
    return await ProvisioningService(provider, default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


async def collect(source: object) -> bytes:
    buffer = bytearray()
    async for piece in source:  # type: ignore[attr-defined]
        buffer.extend(piece)
    return bytes(buffer)


def folder(name: str, default: EncryptionDefault) -> Node:
    return Node(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        kind=NodeKind.FOLDER,
        name=name,
        parent_id=uuid.uuid4(),
        created_at=NOW,
        updated_at=NOW,
        encryption_default=default,
    )


# --- inheritance rules -----------------------------------------------------


def test_off_by_default() -> None:
    assert not resolve_encryption((), requested=None, default_on=False)


def test_the_deployment_default_applies_when_nothing_else_says() -> None:
    assert resolve_encryption((), requested=None, default_on=True)


def test_an_explicit_request_wins_over_everything() -> None:
    chain = (folder("a", EncryptionDefault.ON),)
    assert not resolve_encryption(chain, requested=False, default_on=True)
    assert resolve_encryption((), requested=True, default_on=False)


def test_a_folder_default_is_inherited() -> None:
    assert resolve_encryption(
        (folder("a", EncryptionDefault.ON),), requested=None, default_on=False
    )


def test_the_nearest_ancestor_wins() -> None:
    """Ancestors arrive root-first, so the closest opinion is the last one."""
    chain = (folder("root", EncryptionDefault.ON), folder("inner", EncryptionDefault.OFF))
    assert not resolve_encryption(chain, requested=None, default_on=True)


def test_inherit_passes_the_question_upward() -> None:
    chain = (
        folder("root", EncryptionDefault.ON),
        folder("mid", EncryptionDefault.INHERIT),
        folder("leaf", EncryptionDefault.INHERIT),
    )
    assert resolve_encryption(chain, requested=None, default_on=False)


def test_a_folder_can_turn_encryption_off_below_an_on_ancestor() -> None:
    chain = (folder("root", EncryptionDefault.ON), folder("public", EncryptionDefault.OFF))
    assert not resolve_encryption(chain, requested=None, default_on=False)


# --- end-to-end encrypted content ------------------------------------------


@pytest.fixture
async def world() -> tuple[
    FakeUnitOfWork, User, FakeObjectStore, ContentService, EncryptionService
]:
    provider = keys()
    uow = FakeUnitOfWork()
    user = await provision(uow, provider, "alice")
    store = FakeObjectStore()
    enc = encryption(provider)
    return uow, user, store, files(store, enc), enc


World = tuple[FakeUnitOfWork, User, FakeObjectStore, ContentService, EncryptionService]


async def test_an_encrypted_upload_round_trips(world: World) -> None:
    uow, user, _, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )

    assert node.encrypted
    plan = await content.download(uow, user, node.id)
    assert await collect(plan.stream) == PAYLOAD


async def test_the_stored_object_holds_no_plaintext(world: World) -> None:
    """`content-encryption/spec.md`: reading the object directly yields nothing."""
    uow, user, store, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )

    blob = store.objects[f"{node.owner_id}/{node.id}/{node.current_version_id}"]
    assert b"confidential" not in blob
    assert PAYLOAD[:32] not in blob


async def test_an_unencrypted_upload_is_stored_as_is(world: World) -> None:
    uow, user, store, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "open.txt", stream(PAYLOAD), encrypted=False, now=NOW
    )

    assert not node.encrypted
    assert store.objects[f"{node.owner_id}/{node.id}/{node.current_version_id}"] == PAYLOAD


async def test_the_reported_size_is_the_plaintext_size(world: World) -> None:
    """Content-Length must describe what the caller receives."""
    uow, user, store, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )

    assert node.size_bytes == len(PAYLOAD)
    stored = store.objects[f"{node.owner_id}/{node.id}/{node.current_version_id}"]
    assert len(stored) > len(PAYLOAD), "ciphertext carries header and tags"


async def test_a_range_read_of_encrypted_content(world: World) -> None:
    uow, user, _, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "secret.bin", stream(PAYLOAD), encrypted=True, now=NOW
    )

    plan = await content.download(uow, user, node.id, range_header="bytes=100-149")
    assert await collect(plan.stream) == PAYLOAD[100:150]


async def test_an_empty_encrypted_file_round_trips(world: World) -> None:
    uow, user, _, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "empty.txt", stream(b""), encrypted=True, now=NOW
    )

    plan = await content.download(uow, user, node.id)
    assert await collect(plan.stream) == b""


async def test_each_version_gets_its_own_data_key(world: World) -> None:
    """So a frame from one version can never be replayed into another."""
    uow, user, _, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "a.txt", stream(b"first"), encrypted=True, now=NOW
    )
    await content.replace(uow, user, node.id, stream(b"second"), now=LATER)

    wrapped = await uow.keys.list_data_keys(node.id)
    assert len({key.wrapped_dek for key in wrapped}) == len(wrapped)


async def test_a_folder_default_encrypts_new_uploads(world: World) -> None:
    uow, user, store, content, _ = world
    secret = await nodes().create_folder(
        uow, user, user.root_folder_id, "secret", encryption_default=EncryptionDefault.ON, now=NOW
    )

    node = await content.upload(
        uow, user, secret.node.id, "inherited.txt", stream(PAYLOAD), now=NOW
    )

    assert node.encrypted
    assert PAYLOAD[:32] not in store.objects[f"{node.owner_id}/{node.id}/{node.current_version_id}"]


async def test_an_override_against_the_folder_default_is_audited(world: World) -> None:
    """A downgrade must never be silent."""
    uow, user, _, content, _ = world
    secret = await nodes().create_folder(
        uow, user, user.root_folder_id, "secret", encryption_default=EncryptionDefault.ON, now=NOW
    )

    node = await content.upload(
        uow, user, secret.node.id, "public.txt", stream(PAYLOAD), encrypted=False, now=NOW
    )

    assert not node.encrypted
    assert any(record.action is AuditAction.ENCRYPTION_OVERRIDDEN for record in uow.audit.records)


async def test_changing_a_folder_default_leaves_existing_files_alone(world: World) -> None:
    uow, user, _, content, _ = world
    plain = await nodes().create_folder(uow, user, user.root_folder_id, "docs", now=NOW)
    node = await content.upload(uow, user, plain.node.id, "old.txt", stream(PAYLOAD), now=NOW)
    assert not node.encrypted

    stored = await uow.nodes.get(plain.node.id)
    assert stored is not None
    stored.encryption_default = EncryptionDefault.ON

    unchanged = await uow.nodes.get(node.id)
    assert unchanged is not None and not unchanged.encrypted


# --- key access follows sharing --------------------------------------------


async def test_a_recipient_gets_their_own_wrapped_key() -> None:
    provider = keys()
    uow = FakeUnitOfWork()
    alice = await provision(uow, provider, "alice")
    await provision(uow, provider, "bob")
    enc = encryption(provider)
    store = FakeObjectStore()
    content = files(store, enc)
    sharing = SharingService(FakeDirectory(), keys=enc)

    node = await content.upload(
        uow, alice, alice.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )
    await sharing.grant(uow, alice, node.id, "bob", Role.VIEWER, now=NOW)

    wrapped = await uow.keys.get_data_key(node.id, "bob")
    assert wrapped is not None


async def test_the_recipient_can_actually_decrypt() -> None:
    provider = keys()
    uow = FakeUnitOfWork()
    alice = await provision(uow, provider, "alice")
    bob = await provision(uow, provider, "bob")
    enc = encryption(provider)
    content = files(FakeObjectStore(), enc)
    sharing = SharingService(FakeDirectory(), keys=enc)

    node = await content.upload(
        uow, alice, alice.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )
    await sharing.grant(uow, alice, node.id, "bob", Role.VIEWER, now=NOW)

    plan = await content.download(uow, bob, node.id)
    assert await collect(plan.stream) == PAYLOAD


async def test_the_recipients_copy_is_wrapped_differently() -> None:
    """Same DEK, different KEK -- the content object is untouched."""
    provider = keys()
    uow = FakeUnitOfWork()
    alice = await provision(uow, provider, "alice")
    await provision(uow, provider, "bob")
    enc = encryption(provider)
    store = FakeObjectStore()
    content = files(store, enc)
    sharing = SharingService(FakeDirectory(), keys=enc)

    node = await content.upload(
        uow, alice, alice.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )
    before = dict(store.objects)
    await sharing.grant(uow, alice, node.id, "bob", Role.VIEWER, now=NOW)

    hers = await uow.keys.get_data_key(node.id, "alice")
    his = await uow.keys.get_data_key(node.id, "bob")
    assert hers is not None and his is not None
    assert hers.wrapped_dek != his.wrapped_dek
    assert store.objects == before, "sharing must not rewrite content"


async def test_revocation_destroys_the_recipients_key() -> None:
    provider = keys()
    uow = FakeUnitOfWork()
    alice = await provision(uow, provider, "alice")
    await provision(uow, provider, "bob")
    enc = encryption(provider)
    content = files(FakeObjectStore(), enc)
    sharing = SharingService(FakeDirectory(), keys=enc)

    node = await content.upload(
        uow, alice, alice.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )
    await sharing.grant(uow, alice, node.id, "bob", Role.VIEWER, now=NOW)
    await sharing.revoke(uow, alice, node.id, "bob", now=LATER)

    assert await uow.keys.get_data_key(node.id, "bob") is None
    with pytest.raises(KeyUnavailableError):
        await enc.data_key_for(uow, node, "bob")


async def test_a_stranger_has_no_key(world: World) -> None:
    uow, user, _, content, enc = world
    node = await content.upload(
        uow, user, user.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )

    with pytest.raises(KeyUnavailableError):
        await enc.data_key_for(uow, node, "mallory")


# --- rotation --------------------------------------------------------------


async def test_master_key_rotation_rewraps_every_kek() -> None:
    uow = FakeUnitOfWork()
    old = keys(MASTER_A)
    await provision(uow, old, "alice")
    await provision(uow, old, "bob")

    rotating = encryption(keys(MASTER_B, previous=MASTER_A))
    result = await rotating.rotate_master_key(uow, now=LATER)

    assert result.rewrapped == 2
    expected = master_key_id(MASTER_B)
    for user in uow.users.by_id.values():
        stored = await uow.keys.get_user_key(user.id)
        assert stored is not None
        assert stored.master_key_id == expected


async def test_rotation_leaves_content_readable() -> None:
    uow = FakeUnitOfWork()
    old_provider = keys(MASTER_A)
    alice = await provision(uow, old_provider, "alice")
    store = FakeObjectStore()
    content = files(store, encryption(old_provider))
    node = await content.upload(
        uow, alice, alice.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )
    before = dict(store.objects)

    rotating = encryption(keys(MASTER_B, previous=MASTER_A))
    await rotating.rotate_master_key(uow, now=LATER)

    assert store.objects == before, "content objects are never touched"
    after = files(store, rotating)
    plan = await after.download(uow, alice, node.id)
    assert await collect(plan.stream) == PAYLOAD


async def test_rotation_is_resumable() -> None:
    """Rerunning finishes what an interruption left, and skips what is done."""
    uow = FakeUnitOfWork()
    await provision(uow, keys(MASTER_A), "alice")
    rotating = encryption(keys(MASTER_B, previous=MASTER_A))

    first = await rotating.rotate_master_key(uow, now=LATER)
    second = await rotating.rotate_master_key(uow, now=LATER)

    assert first.rewrapped == 1
    assert second.rewrapped == 0, "already-rotated keys are not touched again"


async def test_rotation_is_audited() -> None:
    uow = FakeUnitOfWork()
    await provision(uow, keys(MASTER_A), "alice")
    rotating = encryption(keys(MASTER_B, previous=MASTER_A))

    await rotating.rotate_master_key(uow, now=LATER)

    assert any(r.action is AuditAction.KEY_ROTATED for r in uow.audit.records)


async def test_user_key_rotation_rewraps_data_keys() -> None:
    provider = keys()
    uow = FakeUnitOfWork()
    alice = await provision(uow, provider, "alice")
    enc = encryption(provider)
    store = FakeObjectStore()
    content = files(store, enc)
    node = await content.upload(
        uow, alice, alice.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )
    before = await uow.keys.get_data_key(node.id, "alice")

    await enc.rotate_user_key(uow, alice.id, now=LATER)

    after = await uow.keys.get_data_key(node.id, "alice")
    assert before is not None and after is not None
    assert after.wrapped_dek != before.wrapped_dek


async def test_content_survives_a_user_key_rotation() -> None:
    provider = keys()
    uow = FakeUnitOfWork()
    alice = await provision(uow, provider, "alice")
    enc = encryption(provider)
    store = FakeObjectStore()
    content = files(store, enc)
    node = await content.upload(
        uow, alice, alice.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )

    await enc.rotate_user_key(uow, alice.id, now=LATER)

    plan = await content.download(uow, alice, node.id)
    assert await collect(plan.stream) == PAYLOAD


# --- conversion ------------------------------------------------------------


async def test_a_plaintext_file_can_be_encrypted(world: World) -> None:
    uow, user, store, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "a.txt", stream(PAYLOAD), encrypted=False, now=NOW
    )

    converted = await content.set_encryption(uow, user, node.id, encrypted=True, now=LATER)

    assert converted.encrypted
    blob = store.objects[f"{converted.owner_id}/{converted.id}/{converted.current_version_id}"]
    assert PAYLOAD[:32] not in blob


async def test_content_survives_being_encrypted(world: World) -> None:
    uow, user, _, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "a.txt", stream(PAYLOAD), encrypted=False, now=NOW
    )
    await content.set_encryption(uow, user, node.id, encrypted=True, now=LATER)

    plan = await content.download(uow, user, node.id)
    assert await collect(plan.stream) == PAYLOAD


async def test_an_encrypted_file_can_be_decrypted(world: World) -> None:
    uow, user, store, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "a.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )

    converted = await content.set_encryption(uow, user, node.id, encrypted=False, now=LATER)

    assert not converted.encrypted
    blob = store.objects[f"{converted.owner_id}/{converted.id}/{converted.current_version_id}"]
    assert blob == PAYLOAD


async def test_decrypting_destroys_the_wrapped_keys(world: World) -> None:
    """Keeping key material for plaintext content serves no purpose."""
    uow, user, _, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "a.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )

    await content.set_encryption(uow, user, node.id, encrypted=False, now=LATER)

    assert await uow.keys.list_data_keys(node.id) == ()


async def test_both_conversions_are_audited(world: World) -> None:
    uow, user, _, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "a.txt", stream(PAYLOAD), encrypted=False, now=NOW
    )

    await content.set_encryption(uow, user, node.id, encrypted=True, now=LATER)
    await content.set_encryption(uow, user, node.id, encrypted=False, now=LATER)

    actions = [r.action for r in uow.audit.records]
    assert AuditAction.ENCRYPTION_ENABLED in actions
    assert AuditAction.ENCRYPTION_DISABLED in actions


async def test_converting_to_the_same_state_is_a_no_op(world: World) -> None:
    uow, user, _, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "a.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )
    before = node.current_version_id

    unchanged = await content.set_encryption(uow, user, node.id, encrypted=True, now=LATER)

    assert unchanged.current_version_id == before


async def test_an_editor_cannot_change_encryption_state() -> None:
    """Only an owner may lower protection."""
    from cyberfs.domain.errors import PermissionDeniedError
    from cyberfs.domain.sharing import Grant

    provider = keys()
    uow = FakeUnitOfWork()
    alice = await provision(uow, provider, "alice")
    bob = await provision(uow, provider, "bob")
    enc = encryption(provider)
    content = files(FakeObjectStore(), enc)
    node = await content.upload(
        uow, alice, alice.root_folder_id, "a.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )
    assert bob.subject == "bob"
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=node.id,
            subject="bob",
            role=Role.EDITOR,
            granted_by="alice",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    with pytest.raises(PermissionDeniedError):
        await content.set_encryption(uow, bob, node.id, encrypted=False, now=LATER)


# --- readiness -------------------------------------------------------------


async def test_the_master_key_verifies_against_stored_material() -> None:
    provider = keys()
    uow = FakeUnitOfWork()
    await provision(uow, provider, "alice")

    assert await encryption(provider).verify_master_key(uow)


async def test_a_wrong_master_key_fails_verification() -> None:
    """Readiness must fail, not surface a 500 per encrypted file."""
    uow = FakeUnitOfWork()
    await provision(uow, keys(MASTER_A), "alice")

    assert not await encryption(keys(MASTER_B)).verify_master_key(uow)


async def test_an_empty_deployment_verifies_trivially() -> None:
    assert await encryption().verify_master_key(FakeUnitOfWork())


# --- copied ciphertext keeps the id that opens it ---------------------------


async def test_restoring_a_version_of_an_encrypted_file_decrypts(world: World) -> None:
    """The regression test for the copy-sealing defect, with real AES-GCM.

    `seal` binds the version id into the AEAD, and a restore writes a new row for
    the *source's* bytes. Authenticating against the new row's own id -- which is
    what the code did -- fails the tag and the download decrypts to nothing, so
    rolling back an encrypted file silently emptied it. The fake object store and
    real cipher are enough to show it: the failure is in the associated data, not
    in the storage.
    """
    uow, user, _, content, _ = world
    node = await content.upload(
        uow, user, user.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )
    first = node.current_version_id
    await content.replace(uow, user, node.id, stream(PAYLOAD + b"-v2"), now=LATER)

    await content.restore_version(uow, user, node.id, first, now=LATER)  # type: ignore[arg-type]

    plan = await content.download(uow, user, node.id)
    assert await collect(plan.stream) == PAYLOAD


async def test_a_copy_of_an_encrypted_file_decrypts(world: World) -> None:
    """The same defect on the other path, behind `POST /nodes/{id}/copy`."""
    uow, user, _, content, _ = world
    source = await content.upload(
        uow, user, user.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )

    view = await nodes().copy(
        uow, user, source.id, user.root_folder_id, name="copy.txt", content=content, now=LATER
    )

    plan = await content.download(uow, user, view.node.id)
    assert await collect(plan.stream) == PAYLOAD


async def test_a_copy_is_byte_identical_in_the_store_so_nothing_was_re_encrypted(
    world: World,
) -> None:
    """The point of recording the sealing id rather than re-sealing on copy.

    Re-encryption under a fresh nonce could not produce identical ciphertext, so
    equality here is proof that the copy stayed a metadata operation instead of
    becoming an O(size) crypto one.
    """
    uow, user, store, content, _ = world
    source = await content.upload(
        uow, user, user.root_folder_id, "secret.txt", stream(PAYLOAD), encrypted=True, now=NOW
    )

    view = await nodes().copy(
        uow, user, source.id, user.root_folder_id, name="copy.txt", content=content, now=LATER
    )

    source_version = await uow.versions.get(source.current_version_id)  # type: ignore[arg-type]
    copied_version = await uow.versions.get(view.node.current_version_id)  # type: ignore[arg-type]
    assert source_version is not None and copied_version is not None
    assert store.objects[copied_version.object_key] == store.objects[source_version.object_key]
    assert copied_version.seal_version_id == source_version.seal_version_id
