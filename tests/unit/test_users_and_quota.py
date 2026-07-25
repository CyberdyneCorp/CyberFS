"""User records and quota accounting."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from cyberfs.domain.auth.principal import Org
from cyberfs.domain.errors import QuotaExceededError
from cyberfs.domain.users import QuotaUsage, User

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
GB = 1024**3
ORG_A = Org(id="org-a", short_name="alpha")
ORG_B = Org(id="org-b", short_name="beta")


def user(**kw: object) -> User:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "subject": "user-1",
        "root_folder_id": uuid.uuid4(),
        "quota_bytes": 10 * GB,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return User(**{**base, **kw})  # type: ignore[arg-type]


def usage(**kw: object) -> QuotaUsage:
    return QuotaUsage(user_id=uuid.uuid4(), **kw)  # type: ignore[arg-type]


# --- claim refresh ---------------------------------------------------------


def test_refresh_reports_a_change() -> None:
    u = user()
    assert u.refresh_from_claims(is_admin=True, org=ORG_A, orgs=(ORG_A,), now=NOW)
    assert u.is_admin
    assert u.org == ORG_A


def test_refresh_reports_no_change_when_claims_match() -> None:
    u = user(is_admin=True, org=ORG_A, orgs=(ORG_A,))
    assert not u.refresh_from_claims(is_admin=True, org=ORG_A, orgs=(ORG_A,), now=NOW)


def test_refresh_always_records_last_seen() -> None:
    u = user()
    u.refresh_from_claims(is_admin=False, org=None, orgs=(), now=NOW)
    assert u.last_seen_at == NOW


def test_demotion_is_picked_up() -> None:
    u = user(is_admin=True)
    assert u.refresh_from_claims(is_admin=False, org=None, orgs=(), now=NOW)
    assert not u.is_admin


def test_org_membership_change_is_picked_up() -> None:
    u = user(orgs=(ORG_A,))
    assert u.refresh_from_claims(is_admin=False, org=ORG_B, orgs=(ORG_B,), now=NOW)
    assert u.orgs == (ORG_B,)


# --- totals ----------------------------------------------------------------


def test_total_sums_every_bucket() -> None:
    assert usage(live_bytes=100, trashed_bytes=20, version_bytes=5).total_bytes == 125


def test_remaining_never_goes_negative() -> None:
    assert usage(live_bytes=200).remaining(100) == 0


def test_remaining_reports_headroom() -> None:
    assert usage(live_bytes=40).remaining(100) == 60


# --- admission control -----------------------------------------------------


def test_upload_within_quota_is_allowed() -> None:
    usage(live_bytes=10).ensure_room_for(100, 50)


def test_upload_exactly_filling_the_quota_is_allowed() -> None:
    usage(live_bytes=50).ensure_room_for(100, 50)


def test_upload_exceeding_the_quota_is_refused() -> None:
    with pytest.raises(QuotaExceededError):
        usage(live_bytes=60).ensure_room_for(100, 50)


def test_trashed_bytes_count_against_the_quota() -> None:
    """Deleting does not free space until purge -- `file-storage/spec.md`."""
    with pytest.raises(QuotaExceededError):
        usage(live_bytes=10, trashed_bytes=80).ensure_room_for(100, 50)


def test_retained_versions_count_against_the_quota() -> None:
    with pytest.raises(QuotaExceededError):
        usage(live_bytes=10, version_bytes=80).ensure_room_for(100, 50)


def test_quota_error_carries_context_without_content() -> None:
    with pytest.raises(QuotaExceededError) as exc:
        usage(live_bytes=90).ensure_room_for(100, 50)
    assert exc.value.context["quota_bytes"] == 100
    assert exc.value.context["used_bytes"] == 90


# --- transitions -----------------------------------------------------------


def test_charging_live_bytes() -> None:
    u = usage()
    u.charge_live(500, NOW)
    assert u.live_bytes == 500
    assert u.updated_at == NOW


def test_releasing_more_than_held_floors_at_zero() -> None:
    """A retried release must not manufacture free space."""
    u = usage(live_bytes=100)
    u.charge_live(-500, NOW)
    assert u.live_bytes == 0


def test_soft_delete_moves_bytes_to_trash_without_freeing_them() -> None:
    u = usage(live_bytes=100)
    u.move_to_trash(60, NOW)
    assert (u.live_bytes, u.trashed_bytes) == (40, 60)
    assert u.total_bytes == 100


def test_trashing_more_than_live_is_clamped() -> None:
    u = usage(live_bytes=30)
    u.move_to_trash(100, NOW)
    assert (u.live_bytes, u.trashed_bytes) == (0, 30)


def test_restore_moves_bytes_back() -> None:
    u = usage(live_bytes=40, trashed_bytes=60)
    u.restore_from_trash(60, NOW)
    assert (u.live_bytes, u.trashed_bytes) == (100, 0)


def test_restoring_more_than_trashed_is_clamped() -> None:
    u = usage(trashed_bytes=10)
    u.restore_from_trash(100, NOW)
    assert (u.live_bytes, u.trashed_bytes) == (10, 0)


def test_purge_is_the_only_transition_that_frees_space() -> None:
    u = usage(trashed_bytes=60)
    u.purge_from_trash(60, NOW)
    assert u.total_bytes == 0


def test_purging_more_than_trashed_floors_at_zero() -> None:
    u = usage(trashed_bytes=10)
    u.purge_from_trash(100, NOW)
    assert u.trashed_bytes == 0


def test_version_bytes_are_charged_and_released() -> None:
    u = usage()
    u.charge_versions(500, NOW)
    u.charge_versions(-200, NOW)
    assert u.version_bytes == 300


# --- reconciliation --------------------------------------------------------


def test_reconcile_reports_and_corrects_drift() -> None:
    u = usage(live_bytes=999)
    assert u.reconcile(live_bytes=100, trashed_bytes=20, version_bytes=5, now=NOW)
    assert u.total_bytes == 125


def test_reconcile_reports_no_drift_when_already_correct() -> None:
    u = usage(live_bytes=100, trashed_bytes=20, version_bytes=5)
    assert not u.reconcile(live_bytes=100, trashed_bytes=20, version_bytes=5, now=NOW)
