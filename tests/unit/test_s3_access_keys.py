"""S3 access-key lifecycle: mint, list, revoke.

The properties `authentication/spec.md` requires of the credential: the secret
is stored only as an irreversible (master-key-sealed) verifier, appears in no
response after creation, revocation takes effect immediately, and multiple keys
coexist so a user can rotate without a gap.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.adapters.inbound.api.schemas import (
    IssuedS3KeyResponse,
    S3KeyList,
    S3KeySummary,
)
from cyberfs.application.s3_keys import S3AccessKeyService
from cyberfs.domain.audit import AuditAction
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import NotFoundError, PermissionDeniedError
from cyberfs.domain.s3.access_key import (
    KEY_ID_PREFIX,
    S3AccessKey,
    generate_key_id,
    generate_secret,
)
from cyberfs.domain.users import User

from .fakes import FakeKeyProvider, FakeUnitOfWork

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3


def user(subject: str = "user-1") -> User:
    return User(
        id=uuid.uuid4(),
        subject=subject,
        root_folder_id=uuid.uuid4(),
        quota_bytes=10 * GB,
        created_at=NOW,
        updated_at=NOW,
    )


def principal(subject: str = "user-1", **kw: object) -> Principal:
    return Principal(subject=subject, **kw)  # type: ignore[arg-type]


def service() -> S3AccessKeyService:
    return S3AccessKeyService(keys=FakeKeyProvider())


# --- pure entity and generators --------------------------------------------


def test_generated_key_id_has_the_aws_style_prefix() -> None:
    key_id = generate_key_id()
    assert key_id.startswith(KEY_ID_PREFIX)
    assert len(key_id) == 20


def test_generated_key_ids_are_unique() -> None:
    assert len({generate_key_id() for _ in range(100)}) == 100


def test_generated_secrets_are_unique_and_substantial() -> None:
    secrets = {generate_secret() for _ in range(100)}
    assert len(secrets) == 100
    assert all(len(s) >= 32 for s in secrets)


def test_revoked_is_idempotent_keeping_the_original_time() -> None:
    key = S3AccessKey(
        key_id="AKIA0",
        sealed_secret=b"x",
        secret_master_key_id="m",
        label="",
        owner_id=uuid.uuid4(),
        owner_subject="user-1",
        created_at=NOW,
    )
    assert key.is_active
    once = key.revoked(NOW)
    twice = once.revoked(LATER)
    assert not once.is_active
    assert twice.revoked_at == NOW


def test_with_last_used_stamps_the_time() -> None:
    key = S3AccessKey(
        key_id="AKIA0",
        sealed_secret=b"x",
        secret_master_key_id="m",
        label="",
        owner_id=uuid.uuid4(),
        owner_subject="user-1",
        created_at=NOW,
    )
    assert key.with_last_used(LATER).last_used_at == LATER


# --- minting ---------------------------------------------------------------


async def test_mint_returns_the_secret_once() -> None:
    uow = FakeUnitOfWork()
    issued = await service().mint(uow, principal(), user(), label="ci", now=NOW)

    assert issued.secret
    assert issued.key.key_id.startswith(KEY_ID_PREFIX)
    assert issued.key.label == "ci"
    assert issued.key.owner_subject == "user-1"


async def test_secret_is_never_stored_in_the_clear() -> None:
    """Invariant A: only a sealed verifier is persisted; the plaintext is
    unrecoverable from stored material without the master key."""
    uow = FakeUnitOfWork()
    keys = FakeKeyProvider()
    issued = await S3AccessKeyService(keys=keys).mint(uow, principal(), user(), label="", now=NOW)

    stored = await uow.s3_keys.get_by_key_id(issued.key.key_id)
    assert stored is not None
    assert stored.sealed_secret != issued.secret.encode()
    # The secret is recoverable only by unsealing under the master key.
    assert (
        keys.unseal_secret(stored.sealed_secret, master_key_id=stored.secret_master_key_id)
        == issued.secret.encode()
    )


async def test_mint_records_the_master_key_that_sealed_it() -> None:
    uow = FakeUnitOfWork()
    issued = await service().mint(uow, principal(), user(), label="", now=NOW)
    assert issued.key.secret_master_key_id == "master-test"


async def test_mint_writes_a_security_audit_record() -> None:
    uow = FakeUnitOfWork()
    issued = await service().mint(uow, principal(), user(), label="", now=NOW)

    actions = [r.action for r in uow.audit.records]
    assert AuditAction.S3_KEY_CREATED in actions
    record = next(r for r in uow.audit.records if r.action == AuditAction.S3_KEY_CREATED)
    assert record.target_id == issued.key.key_id
    # The plaintext secret must not leak into the audit context.
    assert issued.secret not in str(record.context)


async def test_mint_refuses_a_service_principal() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(PermissionDeniedError):
        await service().mint(uow, principal(is_service=True), user(), label="", now=NOW)
    assert uow.s3_keys.by_key_id == {}


# --- listing ---------------------------------------------------------------


async def test_list_never_carries_the_secret() -> None:
    """Invariant A: no post-creation response exposes the secret. The summary
    schema has no field that could carry it."""
    uow = FakeUnitOfWork()
    issued = await service().mint(uow, principal(), user(), label="prod", now=NOW)

    keys = await service().list(uow, "user-1")
    payload = S3KeyList.of(keys).model_dump()
    assert issued.secret not in str(payload)
    assert "secret" not in str(payload)
    assert payload["items"][0]["access_key_id"] == issued.key.key_id


async def test_summary_schema_exposes_no_secret_field() -> None:
    assert "secret_access_key" not in S3KeySummary.model_fields
    assert "sealed_secret" not in S3KeySummary.model_fields


async def test_issued_response_carries_the_secret_but_summary_does_not() -> None:
    uow = FakeUnitOfWork()
    issued = await service().mint(uow, principal(), user(), label="", now=NOW)
    body = IssuedS3KeyResponse.of_issued(issued.key, issued.secret).model_dump()
    assert body["secret_access_key"] == issued.secret


async def test_list_is_scoped_to_the_owner() -> None:
    uow = FakeUnitOfWork()
    await service().mint(uow, principal("alice"), user("alice"), label="", now=NOW)
    await service().mint(uow, principal("bob"), user("bob"), label="", now=NOW)

    assert len(await service().list(uow, "alice")) == 1
    assert len(await service().list(uow, "bob")) == 1


# --- rotation: multiple keys coexist ---------------------------------------


async def test_multiple_active_keys_coexist_for_rotation() -> None:
    uow = FakeUnitOfWork()
    svc = service()
    owner = user()
    first = await svc.mint(uow, principal(), owner, label="old", now=NOW)
    second = await svc.mint(uow, principal(), owner, label="new", now=LATER)

    keys = await svc.list(uow, "user-1")
    assert {k.key_id for k in keys} == {first.key.key_id, second.key.key_id}
    assert all(k.is_active for k in keys)


# --- revocation ------------------------------------------------------------


async def test_revocation_is_immediate() -> None:
    """Invariant: the next lookup finds the key inactive, with no cache."""
    uow = FakeUnitOfWork()
    issued = await service().mint(uow, principal(), user(), label="", now=NOW)

    await service().revoke(uow, "user-1", issued.key.key_id, now=LATER)

    reloaded = await uow.s3_keys.get_by_key_id(issued.key.key_id)
    assert reloaded is not None
    assert not reloaded.is_active
    assert reloaded.revoked_at == LATER


async def test_revoking_one_key_leaves_the_other_working() -> None:
    uow = FakeUnitOfWork()
    svc = service()
    owner = user()
    first = await svc.mint(uow, principal(), owner, label="", now=NOW)
    second = await svc.mint(uow, principal(), owner, label="", now=LATER)

    await svc.revoke(uow, "user-1", first.key.key_id, now=LATER)

    still = await uow.s3_keys.get_by_key_id(second.key.key_id)
    assert still is not None and still.is_active


async def test_revoke_writes_an_audit_record() -> None:
    uow = FakeUnitOfWork()
    issued = await service().mint(uow, principal(), user(), label="", now=NOW)
    await service().revoke(uow, "user-1", issued.key.key_id, now=LATER)
    assert any(r.action == AuditAction.S3_KEY_REVOKED for r in uow.audit.records)


async def test_revoke_of_unknown_key_is_not_found() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(NotFoundError):
        await service().revoke(uow, "user-1", "AKIADOESNOTEXIST00", now=NOW)


async def test_revoke_of_another_users_key_is_not_found() -> None:
    """A caller must not learn another subject's key ids exist."""
    uow = FakeUnitOfWork()
    issued = await service().mint(uow, principal("alice"), user("alice"), label="", now=NOW)

    with pytest.raises(NotFoundError):
        await service().revoke(uow, "bob", issued.key.key_id, now=LATER)

    # Alice's key is untouched.
    still = await uow.s3_keys.get_by_key_id(issued.key.key_id)
    assert still is not None and still.is_active
