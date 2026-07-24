"""Roles, grants, and public links."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.domain.sharing import Grant, PublicLink, Role

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


# --- role ordering ---------------------------------------------------------


def test_roles_are_totally_ordered() -> None:
    assert Role.VIEWER < Role.EDITOR < Role.OWNER


def test_max_resolves_the_highest_role() -> None:
    """`max()` is the whole effective-permission rule -- no deny semantics."""
    assert max(Role.VIEWER, Role.EDITOR) is Role.EDITOR
    assert max(Role.EDITOR, Role.VIEWER) is Role.EDITOR
    assert max(Role.VIEWER, Role.OWNER, Role.EDITOR) is Role.OWNER


def test_role_parsing_is_case_insensitive() -> None:
    assert Role.parse("viewer") is Role.VIEWER
    assert Role.parse(" Editor ") is Role.EDITOR


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        Role.parse("superuser")


def test_role_slug_round_trips() -> None:
    for role in Role:
        assert Role.parse(role.slug) is role


# --- capabilities ----------------------------------------------------------


def test_viewer_may_only_read() -> None:
    assert Role.VIEWER.can_read
    assert not Role.VIEWER.can_write
    assert not Role.VIEWER.can_delete
    assert not Role.VIEWER.can_share


def test_editor_may_write_but_not_delete_or_share() -> None:
    assert Role.EDITOR.can_read
    assert Role.EDITOR.can_write
    assert not Role.EDITOR.can_delete
    assert not Role.EDITOR.can_share


def test_editor_cannot_change_encryption_state() -> None:
    """Only an owner may downgrade protection -- `content-encryption/spec.md`."""
    assert not Role.EDITOR.can_change_encryption
    assert Role.OWNER.can_change_encryption


def test_owner_may_do_everything() -> None:
    assert all(
        (Role.OWNER.can_read, Role.OWNER.can_write, Role.OWNER.can_delete, Role.OWNER.can_share)
    )


# --- grants ----------------------------------------------------------------


def grant(role: Role = Role.VIEWER) -> Grant:
    return Grant(
        id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        subject="bob",
        role=role,
        granted_by="alice",
        created_at=NOW,
        updated_at=NOW,
    )


def test_regrant_replaces_the_role_in_place() -> None:
    """A regrant must not create a second grant for the same pair."""
    original = grant(Role.VIEWER)
    updated = original.with_role(Role.EDITOR, NOW + timedelta(minutes=1))
    assert updated.id == original.id
    assert updated.role is Role.EDITOR
    assert updated.created_at == original.created_at
    assert updated.updated_at > original.updated_at


def test_regrant_can_narrow_a_role() -> None:
    assert grant(Role.OWNER).with_role(Role.VIEWER, NOW).role is Role.VIEWER


# --- public links ----------------------------------------------------------


def link(**kw: object) -> PublicLink:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "node_id": uuid.uuid4(),
        "token_hash": "hashed",
        "created_by": "alice",
        "created_at": NOW,
    }
    return PublicLink(**{**base, **kw})  # type: ignore[arg-type]


def test_public_link_is_always_read_only() -> None:
    """There is no way to widen a link beyond viewer."""
    assert link().role is Role.VIEWER
    assert not link().role.can_write


def test_fresh_link_is_usable() -> None:
    assert link().is_usable(NOW)


def test_expired_link_is_not_usable() -> None:
    assert not link(expires_at=NOW - timedelta(seconds=1)).is_usable(NOW)


def test_link_expiring_exactly_now_is_not_usable() -> None:
    assert not link(expires_at=NOW).is_usable(NOW)


def test_link_with_a_future_expiry_is_usable() -> None:
    assert link(expires_at=NOW + timedelta(days=1)).is_usable(NOW)


def test_link_without_an_expiry_never_expires() -> None:
    assert link().is_usable(NOW + timedelta(days=3650))


def test_revocation_takes_effect_immediately() -> None:
    subject = link()
    subject.revoke(NOW)
    assert subject.is_revoked
    assert not subject.is_usable(NOW)


def test_revocation_is_idempotent() -> None:
    subject = link()
    subject.revoke(NOW)
    subject.revoke(NOW + timedelta(hours=1))
    assert subject.revoked_at == NOW


def test_passphrase_requirement_is_reported() -> None:
    assert not link().requires_passphrase
    assert link(passphrase_hash="hashed").requires_passphrase


def test_access_is_counted() -> None:
    subject = link()
    subject.record_access(NOW)
    subject.record_access(NOW + timedelta(minutes=1))
    assert subject.access_count == 2
    assert subject.last_accessed_at == NOW + timedelta(minutes=1)


def test_only_the_token_hash_is_stored() -> None:
    """A database leak must not yield working links."""
    assert not hasattr(link(), "token")
