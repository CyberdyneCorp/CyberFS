"""Operation auditing -- `file-storage/spec.md` "File operations are auditable"
and `activity-reporting/spec.md` "File operations are recorded".

Every create, read, alter, or remove emits a record naming the actor, the node,
the operation, and the protocol; a name is present only when the actor owns the
node; a public-link read is attributed to the link; a denied operation emits no
activity record; and a failed audit write never fails the operation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import structlog

from cyberfs.application.content import ContentService
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.audit import AuditAction, AuditProtocol, AuditRecord
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import NotFoundError, PermissionDeniedError
from cyberfs.domain.sharing import Grant, PublicLink, Role
from cyberfs.domain.users import User

from .fakes import FakeKeyProvider, FakeObjectStore, FakeUnitOfWork, stream

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3
PAYLOAD = b"the quick brown fox" * 3

FILE_OP_ACTIONS = frozenset(
    {
        AuditAction.FILE_UPLOADED,
        AuditAction.FILE_DOWNLOADED,
        AuditAction.NODE_CREATED,
        AuditAction.NODE_RENAMED,
        AuditAction.NODE_MOVED,
        AuditAction.NODE_COPIED,
        AuditAction.NODE_DELETED,
        AuditAction.NODE_RESTORED,
        AuditAction.VERSION_RESTORED,
    }
)


async def provision(uow: FakeUnitOfWork, subject: str = "alice") -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


def content(store: FakeObjectStore) -> ContentService:
    return ContentService(
        store, max_upload_bytes=10 * GB, upload_chunk_bytes=8, version_retention_count=10
    )


def nodes() -> NodeService:
    return NodeService(max_tree_depth=64, page_size_max=1000)


def op_records(uow: FakeUnitOfWork) -> list[AuditRecord]:
    return [r for r in uow.audit.records if r.action in FILE_OP_ACTIONS]


def only(uow: FakeUnitOfWork, action: AuditAction) -> AuditRecord:
    matched = [r for r in uow.audit.records if r.action == action]
    assert len(matched) == 1, f"expected exactly one {action}, got {len(matched)}"
    return matched[0]


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


# --- mutating tree operations ---------------------------------------------


async def test_create_folder_is_recorded() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    view = await nodes().create_folder(uow, user, user.root_folder_id, "reports", now=NOW)

    record = only(uow, AuditAction.NODE_CREATED)
    assert record.actor_subject == user.subject
    assert record.target_id == str(view.node.id)
    assert record.context["name"] == "reports"


async def test_rename_is_recorded() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = nodes()
    view = await svc.create_folder(uow, user, user.root_folder_id, "old", now=NOW)

    await svc.rename(uow, user, view.node.id, "new", now=LATER)

    record = only(uow, AuditAction.NODE_RENAMED)
    assert record.actor_subject == user.subject
    assert record.target_id == str(view.node.id)


async def test_move_is_recorded() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = nodes()
    dest = await svc.create_folder(uow, user, user.root_folder_id, "dest", now=NOW)
    moving = await svc.create_folder(uow, user, user.root_folder_id, "moving", now=NOW)

    await svc.move(uow, user, moving.node.id, dest.node.id, now=LATER)

    record = only(uow, AuditAction.NODE_MOVED)
    assert record.target_id == str(moving.node.id)


async def test_delete_is_recorded() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = nodes()
    folder = await svc.create_folder(uow, user, user.root_folder_id, "doomed", now=NOW)

    await svc.delete(uow, user, folder.node.id, now=LATER)

    record = only(uow, AuditAction.NODE_DELETED)
    assert record.target_id == str(folder.node.id)


async def test_restore_is_recorded() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = nodes()
    folder = await svc.create_folder(uow, user, user.root_folder_id, "doomed", now=NOW)
    await svc.delete(uow, user, folder.node.id, now=LATER)

    await svc.restore(uow, user, folder.node.id, now=LATER)

    record = only(uow, AuditAction.NODE_RESTORED)
    assert record.target_id == str(folder.node.id)


async def test_copy_records_the_root_copy_only() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = nodes()
    branch = await svc.create_folder(uow, user, user.root_folder_id, "branch", now=NOW)
    await svc.create_folder(uow, user, branch.node.id, "leaf", now=NOW)
    dest = await svc.create_folder(uow, user, user.root_folder_id, "dest", now=NOW)

    copied = await svc.copy(uow, user, branch.node.id, dest.node.id, now=LATER)

    # One record for the copy as a whole, not one per child.
    record = only(uow, AuditAction.NODE_COPIED)
    assert record.target_id == str(copied.node.id)
    # A copy is always the caller's, so its name is carried.
    assert record.context["name"] == "branch"


# --- upload and download ---------------------------------------------------


async def test_upload_records_the_plaintext_byte_count() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = content(FakeObjectStore())

    node = await svc.upload(uow, user, user.root_folder_id, "notes.txt", stream(PAYLOAD), now=NOW)

    record = only(uow, AuditAction.FILE_UPLOADED)
    assert record.actor_subject == user.subject
    assert record.target_id == str(node.id)
    assert record.context["bytes"] == len(PAYLOAD)
    assert record.context["name"] == "notes.txt"


async def test_replace_records_an_upload() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = content(FakeObjectStore())
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"one"), now=NOW)

    await svc.replace(uow, user, node.id, stream(b"three!"), now=LATER)

    uploads = [r for r in uow.audit.records if r.action == AuditAction.FILE_UPLOADED]
    assert len(uploads) == 2
    assert uploads[-1].context["bytes"] == len(b"three!")


async def test_download_is_recorded() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = content(FakeObjectStore())
    node = await svc.upload(uow, user, user.root_folder_id, "a.bin", stream(PAYLOAD), now=NOW)

    await svc.download(uow, user, node.id, now=LATER)

    record = only(uow, AuditAction.FILE_DOWNLOADED)
    assert record.actor_subject == user.subject
    assert record.target_id == str(node.id)
    assert record.context["bytes"] == len(PAYLOAD)


async def test_version_restore_is_recorded() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = content(FakeObjectStore())
    node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"first"), now=NOW)
    original = node.current_version_id
    assert original is not None
    await svc.replace(uow, user, node.id, stream(b"second"), now=LATER)

    await svc.restore_version(uow, user, node.id, original, now=LATER)

    record = only(uow, AuditAction.VERSION_RESTORED)
    assert record.target_id == str(node.id)


# --- privacy: name only for the owner, reader named on someone else's file --


async def test_recipient_download_names_the_reader_not_the_file() -> None:
    """`A read of someone else's file names the reader` -- and omits the name."""
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = content(FakeObjectStore())
    node = await svc.upload(
        uow, alice, alice.root_folder_id, "secret.txt", stream(PAYLOAD), now=NOW
    )
    await grant(uow, node.id, bob.subject, Role.VIEWER)

    await svc.download(uow, bob, node.id, now=LATER)

    record = only(uow, AuditAction.FILE_DOWNLOADED)
    assert record.actor_subject == bob.subject
    assert record.target_id == str(node.id)
    # Bob does not own the node, so its name never appears.
    assert "name" not in record.context


async def test_a_public_link_read_is_attributed_to_the_link() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    svc = content(FakeObjectStore())
    node = await svc.upload(
        uow, alice, alice.root_folder_id, "shared.txt", stream(PAYLOAD), now=NOW
    )
    link_id = uuid.uuid4()
    await uow.public_links.add(
        PublicLink(
            id=link_id,
            node_id=node.id,
            token_hash="hash",
            created_by=alice.subject,
            created_at=NOW,
        )
    )

    # The router reads as the owner but attributes to the link.
    await svc.download(uow, alice, node.id, link_id=link_id, now=LATER)

    record = only(uow, AuditAction.FILE_DOWNLOADED)
    assert record.actor_subject is None
    assert record.context["link_id"] == str(link_id)
    assert "name" not in record.context


# --- denials are not activity ----------------------------------------------


async def test_a_denied_download_records_a_denial_not_an_operation() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = content(FakeObjectStore())
    node = await svc.upload(uow, alice, alice.root_folder_id, "private.bin", stream(b"x"), now=NOW)
    uow.audit.records.clear()

    with pytest.raises(NotFoundError):
        await svc.download(uow, bob, node.id, now=LATER)

    # No success record, but the refusal is recorded as an authorization denial
    # naming the actor and the node -- never the file name Bob may not see.
    assert op_records(uow) == []
    denial = only(uow, AuditAction.AUTH_DENIED)
    assert denial.actor_subject == bob.subject
    assert denial.target_id == str(node.id)
    assert "name" not in denial.context


async def test_a_denied_rename_records_a_denial_not_an_operation() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = nodes()
    view = await svc.create_folder(uow, alice, alice.root_folder_id, "a", now=NOW)
    await grant(uow, view.node.id, bob.subject, Role.VIEWER)
    uow.audit.records.clear()

    with pytest.raises(PermissionDeniedError):
        await svc.rename(uow, bob, view.node.id, "b", now=LATER)

    assert op_records(uow) == []
    denial = only(uow, AuditAction.AUTH_DENIED)
    assert denial.actor_subject == bob.subject
    assert denial.target_id == str(view.node.id)
    assert "name" not in denial.context


async def test_a_denied_restore_records_a_denial() -> None:
    """The trash path denies through its own authorization check, too."""
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = nodes()
    view = await svc.create_folder(uow, alice, alice.root_folder_id, "doomed", now=NOW)
    await svc.delete(uow, alice, view.node.id, now=LATER)
    uow.audit.records.clear()

    with pytest.raises(NotFoundError):
        await svc.restore(uow, bob, view.node.id, now=LATER)

    denial = only(uow, AuditAction.AUTH_DENIED)
    assert denial.actor_subject == bob.subject
    assert denial.target_id == str(view.node.id)


async def test_a_failed_audit_write_does_not_surface_a_denial() -> None:
    """Recording the denial is non-blocking: a write failure still denies."""
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    svc = nodes()
    view = await svc.create_folder(uow, alice, alice.root_folder_id, "a", now=NOW)
    await grant(uow, view.node.id, bob.subject, Role.VIEWER)
    uow.audit.fail_add = True

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(PermissionDeniedError):
            await svc.rename(uow, bob, view.node.id, "b", now=LATER)

    assert any(entry["event"] == "audit_write_failed" for entry in logs)


# --- protocol and non-blocking write ---------------------------------------


async def test_records_default_to_the_rest_protocol() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = content(FakeObjectStore())

    await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(b"x"), now=NOW)

    record = only(uow, AuditAction.FILE_UPLOADED)
    assert record.protocol is AuditProtocol.REST


async def test_a_failed_audit_write_does_not_fail_the_upload() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = content(FakeObjectStore())
    uow.audit.fail_add = True

    with structlog.testing.capture_logs() as logs:
        node = await svc.upload(uow, user, user.root_folder_id, "a.txt", stream(PAYLOAD), now=NOW)

    # The file is stored despite the audit failure.
    assert node.size_bytes == len(PAYLOAD)
    assert op_records(uow) == []
    assert any(entry["event"] == "audit_write_failed" for entry in logs)


async def test_a_failed_audit_write_does_not_fail_a_rename() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = nodes()
    view = await svc.create_folder(uow, user, user.root_folder_id, "old", now=NOW)
    uow.audit.fail_add = True

    with structlog.testing.capture_logs() as logs:
        renamed = await svc.rename(uow, user, view.node.id, "new", now=LATER)

    assert renamed.node.name == "new"
    assert any(entry["event"] == "audit_write_failed" for entry in logs)
