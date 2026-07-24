"""Wrapped key material."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from cyberfs.domain.keys import UserKey, WrappedDataKey

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


def user_key(**kw: object) -> UserKey:
    base: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "wrapped_kek": b"sealed-under-master-v1",
        "master_key_id": "master-v1",
        "created_at": NOW,
    }
    return UserKey(**{**base, **kw})  # type: ignore[arg-type]


def data_key(**kw: object) -> WrappedDataKey:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "node_id": uuid.uuid4(),
        "subject": "alice",
        "wrapped_dek": b"sealed-under-alice-kek",
        "created_at": NOW,
    }
    return WrappedDataKey(**{**base, **kw})  # type: ignore[arg-type]


# --- user keys -------------------------------------------------------------


def test_user_key_records_which_master_sealed_it() -> None:
    """Rotation needs to find what still awaits rewrapping."""
    assert user_key().master_key_id == "master-v1"


def test_rewrapping_keeps_the_user_and_creation_time() -> None:
    original = user_key()
    rotated = original.rewrapped(b"sealed-under-master-v2", "master-v2", LATER)
    assert rotated.user_id == original.user_id
    assert rotated.created_at == original.created_at


def test_rewrapping_replaces_the_sealed_material_and_master_id() -> None:
    rotated = user_key().rewrapped(b"sealed-under-master-v2", "master-v2", LATER)
    assert rotated.wrapped_kek == b"sealed-under-master-v2"
    assert rotated.master_key_id == "master-v2"
    assert rotated.rotated_at == LATER


def test_rewrapping_does_not_mutate_the_original() -> None:
    original = user_key()
    original.rewrapped(b"new", "master-v2", LATER)
    assert original.master_key_id == "master-v1"
    assert original.rotated_at is None


# --- data keys -------------------------------------------------------------


def test_data_key_is_scoped_to_a_node_and_subject() -> None:
    key = data_key()
    assert key.subject == "alice"
    assert isinstance(key.node_id, uuid.UUID)


def test_rewrapping_for_a_recipient_targets_the_same_node() -> None:
    """Sharing rewraps key material; the content objects are untouched."""
    owners_key = data_key()
    recipients = owners_key.rewrapped_for("bob", b"sealed-under-bob-kek", LATER, uuid.uuid4())
    assert recipients.node_id == owners_key.node_id
    assert recipients.subject == "bob"
    assert recipients.wrapped_dek == b"sealed-under-bob-kek"


def test_rewrapped_copy_is_a_distinct_row() -> None:
    owners_key = data_key()
    recipients = owners_key.rewrapped_for("bob", b"other", LATER, uuid.uuid4())
    assert recipients.id != owners_key.id
    assert recipients.created_at == LATER


def test_rewrapping_does_not_mutate_the_owners_key() -> None:
    owners_key = data_key()
    owners_key.rewrapped_for("bob", b"other", LATER, uuid.uuid4())
    assert owners_key.subject == "alice"
    assert owners_key.wrapped_dek == b"sealed-under-alice-kek"


def test_only_wrapped_material_is_held() -> None:
    """No attribute anywhere exposes an unwrapped key."""
    for key in (user_key(), data_key()):
        assert not any("unwrapped" in slot or slot in {"dek", "kek"} for slot in key.__slots__)
