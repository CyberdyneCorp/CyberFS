"""First-touch user provisioning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.provisioning import ROOT_FOLDER_NAME, ProvisioningService
from cyberfs.domain.audit import AuditAction
from cyberfs.domain.auth.principal import Org, Principal
from cyberfs.domain.errors import PermissionDeniedError

from .fakes import FakeKeyProvider, FakeUnitOfWork

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3
ORG_A = Org(id="org-a", short_name="alpha")
ORG_B = Org(id="org-b", short_name="beta")


def principal(subject: str = "user-1", **kw: object) -> Principal:
    return Principal(subject=subject, **kw)  # type: ignore[arg-type]


def service() -> ProvisioningService:
    return ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB)


# --- first touch -----------------------------------------------------------


async def test_first_request_creates_the_user() -> None:
    uow = FakeUnitOfWork()
    user = await service().resolve(uow, principal(), now=NOW)

    assert user.subject == "user-1"
    assert await uow.users.get_by_subject("user-1") is user


async def test_first_request_creates_a_root_folder() -> None:
    uow = FakeUnitOfWork()
    user = await service().resolve(uow, principal(), now=NOW)

    root = await uow.nodes.get(user.root_folder_id)
    assert root is not None
    assert root.is_root
    assert root.is_folder
    assert root.owner_id == user.id
    assert root.name == ROOT_FOLDER_NAME


async def test_first_request_creates_a_wrapped_key() -> None:
    uow = FakeUnitOfWork()
    user = await service().resolve(uow, principal(), now=NOW)

    key = await uow.keys.get_user_key(user.id)
    assert key is not None
    assert key.master_key_id == "master-test"


async def test_stored_key_material_is_wrapped_not_raw() -> None:
    """`content-encryption/spec.md`: only sealed key material is persisted."""
    uow = FakeUnitOfWork()
    user = await service().resolve(uow, principal(), now=NOW)

    key = await uow.keys.get_user_key(user.id)
    assert key is not None
    assert key.wrapped_kek.startswith(b"wrapped:")
    assert key.wrapped_kek != b"kek-1"


async def test_first_request_applies_the_default_quota() -> None:
    uow = FakeUnitOfWork()
    user = await service().resolve(uow, principal(), now=NOW)

    assert user.quota_bytes == 10 * GB
    usage = await uow.quotas.get(user.id)
    assert usage is not None
    assert usage.total_bytes == 0


async def test_provisioning_is_audited() -> None:
    uow = FakeUnitOfWork()
    user = await service().resolve(uow, principal(), now=NOW)

    record = uow.audit.records[-1]
    assert record.action is AuditAction.USER_PROVISIONED
    assert record.target_id == str(user.id)


async def test_claims_are_carried_onto_the_new_record() -> None:
    uow = FakeUnitOfWork()
    user = await service().resolve(uow, principal(is_admin=True, org=ORG_A, orgs=(ORG_A,)), now=NOW)

    assert user.is_admin
    assert user.org == ORG_A
    assert user.orgs == (ORG_A,)


# --- returning callers -----------------------------------------------------


async def test_second_request_reuses_the_same_user() -> None:
    uow = FakeUnitOfWork()
    first = await service().resolve(uow, principal(), now=NOW)
    second = await service().resolve(uow, principal(), now=LATER)

    assert first.id == second.id
    assert len(uow.users.by_id) == 1


async def test_second_request_creates_no_second_root_folder() -> None:
    uow = FakeUnitOfWork()
    subject = service()
    await subject.resolve(uow, principal(), now=NOW)
    await subject.resolve(uow, principal(), now=LATER)

    assert len(uow.nodes.by_id) == 1


async def test_second_request_does_not_regenerate_the_key() -> None:
    """Regenerating would orphan every DEK wrapped under the old KEK."""
    uow = FakeUnitOfWork()
    keys = FakeKeyProvider()
    subject = ProvisioningService(keys, default_quota_bytes=10 * GB)

    await subject.resolve(uow, principal(), now=NOW)
    await subject.resolve(uow, principal(), now=LATER)

    assert keys.generated == 1


async def test_promotion_is_picked_up_on_the_next_request() -> None:
    uow = FakeUnitOfWork()
    subject = service()
    await subject.resolve(uow, principal(), now=NOW)
    user = await subject.resolve(uow, principal(is_admin=True), now=LATER)

    assert user.is_admin


async def test_demotion_is_picked_up_on_the_next_request() -> None:
    uow = FakeUnitOfWork()
    subject = service()
    await subject.resolve(uow, principal(is_admin=True), now=NOW)
    user = await subject.resolve(uow, principal(is_admin=False), now=LATER)

    assert not user.is_admin


async def test_org_membership_change_is_picked_up() -> None:
    uow = FakeUnitOfWork()
    subject = service()
    await subject.resolve(uow, principal(org=ORG_A, orgs=(ORG_A,)), now=NOW)
    user = await subject.resolve(uow, principal(org=ORG_B, orgs=(ORG_B,)), now=LATER)

    assert user.org == ORG_B
    assert user.orgs == (ORG_B,)


async def test_every_request_records_last_seen() -> None:
    uow = FakeUnitOfWork()
    subject = service()
    await subject.resolve(uow, principal(), now=NOW)
    user = await subject.resolve(uow, principal(), now=LATER)

    assert user.last_seen_at == LATER


async def test_identity_survives_an_email_change() -> None:
    """The subject is the identity; nothing else keys the record."""
    uow = FakeUnitOfWork()
    subject = service()
    first = await subject.resolve(uow, principal("user-1"), now=NOW)
    second = await subject.resolve(uow, principal("user-1"), now=LATER)

    assert first.id == second.id
    assert first.root_folder_id == second.root_folder_id


# --- separation ------------------------------------------------------------


async def test_distinct_subjects_get_distinct_trees() -> None:
    uow = FakeUnitOfWork()
    subject = service()
    alice = await subject.resolve(uow, principal("alice"), now=NOW)
    bob = await subject.resolve(uow, principal("bob"), now=NOW)

    assert alice.id != bob.id
    assert alice.root_folder_id != bob.root_folder_id


async def test_service_principal_is_refused_storage() -> None:
    """A service acts on its own behalf and can never own a file."""
    uow = FakeUnitOfWork()
    with pytest.raises(PermissionDeniedError):
        await service().resolve(uow, principal("svc", is_service=True), now=NOW)

    assert uow.users.by_id == {}


async def test_refused_service_principal_leaves_nothing_behind() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(PermissionDeniedError):
        await service().resolve(uow, principal("svc", is_service=True), now=NOW)

    assert uow.nodes.by_id == {}
    assert uow.keys.user_keys == {}
    assert uow.audit.records == []
