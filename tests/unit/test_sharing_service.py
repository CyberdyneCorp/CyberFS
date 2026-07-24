"""Grants, links, and ownership transfer -- `sharing/spec.md`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.content import ContentService
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.sharing import SharingService
from cyberfs.domain.audit import AuditAction
from cyberfs.domain.auth.principal import Org, Principal
from cyberfs.domain.errors import (
    CannotRevokeOwnerError,
    CannotShareWithSelfError,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitedError,
    RecipientUnknownError,
    ValidationError,
)
from cyberfs.domain.sharing import Role
from cyberfs.domain.users import User

from .fakes import FakeKeyProvider, FakeObjectStore, FakeUnitOfWork, stream

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3
PAYLOAD = b"shared bytes" * 8


class FakeDirectory:
    """Resolves a fixed email book; anything UUID-shaped passes through."""

    def __init__(self, book: dict[str, str] | None = None) -> None:
        self.book = book or {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def find_subject(self, identifier: str, *, within_orgs: Sequence[str] = ()) -> str | None:
        self.calls.append((identifier, tuple(within_orgs)))
        if identifier in self.book:
            return self.book[identifier]
        if "@" in identifier:
            return None
        return identifier


async def provision(uow: FakeUnitOfWork, subject: str = "alice", **kw: object) -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow,
        Principal(subject=subject, **kw),
        now=NOW,  # type: ignore[arg-type]
    )


def nodes() -> NodeService:
    return NodeService(max_tree_depth=64, page_size_max=100)


def files(store: FakeObjectStore) -> ContentService:
    return ContentService(
        store, max_upload_bytes=10 * GB, upload_chunk_bytes=8, version_retention_count=10
    )


@pytest.fixture
async def world() -> tuple[FakeUnitOfWork, User, User, SharingService]:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    return uow, alice, bob, SharingService(FakeDirectory())


World = tuple[FakeUnitOfWork, User, User, SharingService]


# --- granting --------------------------------------------------------------


async def test_owner_grants_a_role(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)

    grant = await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)

    assert grant.subject == "bob"
    assert grant.role is Role.VIEWER
    assert grant.granted_by == "alice"


async def test_the_recipient_can_then_read(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)

    view = await nodes().get(uow, bob, folder.node.id)
    assert view.role is Role.VIEWER


async def test_regrant_replaces_rather_than_duplicates(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    first = await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)

    second = await sharing.grant(uow, alice, folder.node.id, "bob", Role.EDITOR, now=LATER)

    assert second.id == first.id
    assert second.role is Role.EDITOR
    assert len(await uow.grants.list_for_node(folder.node.id)) == 1


async def test_a_regrant_can_narrow_a_role(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.OWNER, now=NOW)

    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=LATER)

    assert (await nodes().get(uow, bob, folder.node.id)).role is Role.VIEWER


async def test_sharing_with_yourself_is_refused(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    with pytest.raises(CannotShareWithSelfError):
        await sharing.grant(uow, alice, folder.node.id, "alice", Role.VIEWER, now=NOW)


async def test_an_unknown_recipient_is_refused(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    with pytest.raises(RecipientUnknownError):
        await sharing.grant(uow, alice, folder.node.id, "nobody@example.com", Role.VIEWER, now=NOW)


async def test_an_editor_cannot_re_share(world: World) -> None:
    """`sharing/spec.md`: only an owner may widen access."""
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.EDITOR, now=NOW)

    with pytest.raises(PermissionDeniedError):
        await sharing.grant(uow, bob, folder.node.id, "carol", Role.VIEWER, now=LATER)


async def test_a_stranger_cannot_share(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "private", now=NOW)
    with pytest.raises(NotFoundError):
        await sharing.grant(uow, bob, folder.node.id, "carol", Role.VIEWER, now=NOW)


async def test_email_lookup_is_scoped_to_the_sharers_orgs() -> None:
    """CyberdyneAuth publishes no global email lookup, only org directories."""
    uow = FakeUnitOfWork()
    org = Org(id="org-a", short_name="alpha")
    alice = await provision(uow, "alice", org=org, orgs=(org,))
    await provision(uow, "bob")
    directory = FakeDirectory({"bob@example.com": "bob"})
    sharing = SharingService(directory)
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)

    await sharing.grant(uow, alice, folder.node.id, "bob@example.com", Role.VIEWER, now=NOW)

    assert directory.calls[-1] == ("bob@example.com", ("org-a",))


async def test_granting_is_audited(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)

    record = uow.audit.records[-1]
    assert record.action is AuditAction.GRANT_CREATED
    assert record.actor_subject == "alice"
    assert record.recipient_subject == "bob"
    assert record.context["role"] == "viewer"


# --- inheritance -----------------------------------------------------------


async def test_a_folder_grant_reaches_future_descendants(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)

    # Created *after* the grant.
    inner = await nodes().create_folder(uow, alice, folder.node.id, "later", now=LATER)

    assert (await nodes().get(uow, bob, inner.node.id)).role is Role.VIEWER


async def test_moving_out_of_a_shared_folder_ends_inherited_access(world: World) -> None:
    uow, alice, bob, sharing = world
    shared = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    private = await nodes().create_folder(uow, alice, alice.root_folder_id, "private", now=NOW)
    item = await nodes().create_folder(uow, alice, shared.node.id, "item", now=NOW)
    await sharing.grant(uow, alice, shared.node.id, "bob", Role.VIEWER, now=NOW)
    assert (await nodes().get(uow, bob, item.node.id)).role is Role.VIEWER

    await nodes().move(uow, alice, item.node.id, private.node.id, now=LATER)

    with pytest.raises(NotFoundError):
        await nodes().get(uow, bob, item.node.id)


async def test_moving_into_a_shared_folder_grants_inherited_access(world: World) -> None:
    uow, alice, bob, sharing = world
    shared = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    item = await nodes().create_folder(uow, alice, alice.root_folder_id, "item", now=NOW)
    await sharing.grant(uow, alice, shared.node.id, "bob", Role.VIEWER, now=NOW)

    await nodes().move(uow, alice, item.node.id, shared.node.id, now=LATER)

    assert (await nodes().get(uow, bob, item.node.id)).role is Role.VIEWER


# --- revocation ------------------------------------------------------------


async def test_revocation_takes_effect_immediately(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)

    await sharing.revoke(uow, alice, folder.node.id, "bob", now=LATER)

    with pytest.raises(NotFoundError):
        await nodes().get(uow, bob, folder.node.id)


async def test_a_recipient_may_drop_their_own_access(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)

    await sharing.revoke(uow, bob, folder.node.id, "bob", now=LATER)

    assert await uow.grants.list_for_node(folder.node.id) == ()


async def test_a_recipient_cannot_revoke_someone_else(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "carol", Role.VIEWER, now=NOW)

    with pytest.raises(PermissionDeniedError):
        await sharing.revoke(uow, bob, folder.node.id, "carol", now=LATER)


async def test_the_owner_cannot_be_revoked(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    with pytest.raises(CannotRevokeOwnerError):
        await sharing.revoke(uow, alice, folder.node.id, "alice", now=LATER)


async def test_revoking_a_folder_grant_leaves_an_independent_one(world: World) -> None:
    """`sharing/spec.md`: an independent direct grant survives."""
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    inner = await nodes().create_folder(uow, alice, folder.node.id, "inner", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)
    await sharing.grant(uow, alice, inner.node.id, "bob", Role.EDITOR, now=NOW)

    await sharing.revoke(uow, alice, folder.node.id, "bob", now=LATER)

    with pytest.raises(NotFoundError):
        await nodes().get(uow, bob, folder.node.id)
    assert (await nodes().get(uow, bob, inner.node.id)).role is Role.EDITOR


async def test_revoking_a_missing_grant_is_not_found(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    with pytest.raises(NotFoundError):
        await sharing.revoke(uow, alice, folder.node.id, "nobody", now=LATER)


async def test_revocation_is_audited(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)
    await sharing.revoke(uow, alice, folder.node.id, "bob", now=LATER)

    assert uow.audit.records[-1].action is AuditAction.GRANT_REVOKED


# --- listings --------------------------------------------------------------


async def test_shared_with_me_lists_subtree_roots(world: World) -> None:
    """Not every inherited descendant -- just the root of each share."""
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    inner = await nodes().create_folder(uow, alice, folder.node.id, "inner", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)
    await sharing.grant(uow, alice, inner.node.id, "bob", Role.EDITOR, now=NOW)

    roots = await sharing.shared_with_me(uow, bob)

    assert [n.id for n in roots] == [folder.node.id]


async def test_shared_with_me_is_empty_without_grants(world: World) -> None:
    uow, _, bob, sharing = world
    assert await sharing.shared_with_me(uow, bob) == ()


async def test_shared_with_me_skips_trashed_nodes(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)
    await nodes().delete(uow, alice, folder.node.id, now=LATER)

    assert await sharing.shared_with_me(uow, bob) == ()


async def test_only_the_owner_may_list_grants(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "shared", now=NOW)
    await sharing.grant(uow, alice, folder.node.id, "bob", Role.EDITOR, now=NOW)

    with pytest.raises(PermissionDeniedError):
        await sharing.list_grants(uow, bob, folder.node.id)


# --- public links ----------------------------------------------------------


async def test_a_link_is_issued_with_a_token(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)

    issued = await sharing.create_link(uow, alice, folder.node.id, now=NOW)

    assert issued.token
    assert len(issued.token) >= 22, "at least 128 bits of entropy, base64url-encoded"


async def test_the_token_is_not_stored_in_the_clear(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, now=NOW)

    assert issued.token not in issued.link.token_hash
    assert issued.link.token_hash != issued.token


async def test_the_token_does_not_encode_the_node_id(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, now=NOW)

    assert folder.node.id.hex not in issued.token


async def test_tokens_are_unique(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    tokens = {
        (await sharing.create_link(uow, alice, folder.node.id, now=NOW)).token for _ in range(16)
    }
    assert len(tokens) == 16


async def test_a_link_resolves_to_its_node(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, now=NOW)

    access = await sharing.resolve_link(uow, issued.token, now=NOW)

    assert access.node.id == folder.node.id
    assert access.role is Role.VIEWER, "a link never grants more than read"


async def test_an_unknown_token_is_not_found(world: World) -> None:
    uow, _, _, sharing = world
    with pytest.raises(NotFoundError):
        await sharing.resolve_link(uow, "not-a-real-token", now=NOW)


async def test_an_expired_link_is_not_found(world: World) -> None:
    """404, not 403: an expired link must not confirm it ever existed."""
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(
        uow, alice, folder.node.id, expires_at=NOW + timedelta(minutes=5), now=NOW
    )

    with pytest.raises(NotFoundError):
        await sharing.resolve_link(uow, issued.token, now=NOW + timedelta(hours=1))


async def test_an_expiry_in_the_past_is_refused(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    with pytest.raises(ValidationError):
        await sharing.create_link(
            uow, alice, folder.node.id, expires_at=NOW - timedelta(minutes=1), now=NOW
        )


async def test_a_revoked_link_stops_working_immediately(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, now=NOW)

    await sharing.revoke_link(uow, alice, issued.link.id, now=LATER)

    with pytest.raises(NotFoundError):
        await sharing.resolve_link(uow, issued.token, now=LATER)


async def test_a_link_to_a_trashed_node_is_not_found(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, now=NOW)
    await nodes().delete(uow, alice, folder.node.id, now=LATER)

    with pytest.raises(NotFoundError):
        await sharing.resolve_link(uow, issued.token, now=LATER)


async def test_link_access_is_counted(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, now=NOW)

    await sharing.resolve_link(uow, issued.token, now=NOW)
    await sharing.resolve_link(uow, issued.token, now=LATER)

    stored = await uow.public_links.get(issued.link.id)
    assert stored is not None and stored.access_count == 2


async def test_link_use_is_audited_without_the_secret(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, now=NOW)
    await sharing.resolve_link(uow, issued.token, source_ip="203.0.113.5", now=NOW)

    record = uow.audit.records[-1]
    assert record.action is AuditAction.PUBLIC_LINK_ACCESSED
    assert record.source_ip == "203.0.113.5"
    assert issued.token not in repr(record)


# --- link passphrases ------------------------------------------------------


async def test_a_passphrase_protected_link_needs_the_passphrase(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, passphrase="s3same", now=NOW)

    with pytest.raises(PermissionDeniedError):
        await sharing.resolve_link(uow, issued.token, now=NOW)


async def test_the_correct_passphrase_opens_the_link(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, passphrase="s3same", now=NOW)

    access = await sharing.resolve_link(uow, issued.token, passphrase="s3same", now=NOW)
    assert access.node.id == folder.node.id


async def test_a_wrong_passphrase_is_refused(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, passphrase="s3same", now=NOW)

    with pytest.raises(PermissionDeniedError):
        await sharing.resolve_link(uow, issued.token, passphrase="wrong", now=NOW)


async def test_the_passphrase_is_not_stored_in_the_clear(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, passphrase="s3same", now=NOW)

    stored = issued.link.passphrase_hash or ""
    assert "s3same" not in stored
    assert stored.startswith("scrypt$")


async def test_passphrase_attempts_are_rate_limited(world: World) -> None:
    """A human-chosen passphrase must not be grindable."""
    uow, alice, _, _ = world
    sharing = SharingService(FakeDirectory(), passphrase_attempts_per_min=3)
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, passphrase="s3same", now=NOW)

    for _ in range(3):
        with pytest.raises(PermissionDeniedError):
            await sharing.resolve_link(uow, issued.token, passphrase="wrong", now=NOW)

    with pytest.raises(RateLimitedError):
        await sharing.resolve_link(uow, issued.token, passphrase="wrong", now=NOW)


async def test_the_rate_limit_lifts_after_its_window(world: World) -> None:
    uow, alice, _, _ = world
    sharing = SharingService(FakeDirectory(), passphrase_attempts_per_min=1)
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, passphrase="s3same", now=NOW)

    with pytest.raises(PermissionDeniedError):
        await sharing.resolve_link(uow, issued.token, passphrase="wrong", now=NOW)
    with pytest.raises(RateLimitedError):
        await sharing.resolve_link(uow, issued.token, passphrase="wrong", now=NOW)

    access = await sharing.resolve_link(
        uow, issued.token, passphrase="s3same", now=NOW + timedelta(minutes=2)
    )
    assert access.node.id == folder.node.id


async def test_only_the_owner_may_revoke_a_link(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "public", now=NOW)
    issued = await sharing.create_link(uow, alice, folder.node.id, now=NOW)

    with pytest.raises(NotFoundError):
        await sharing.revoke_link(uow, bob, issued.link.id, now=LATER)


# --- ownership transfer ----------------------------------------------------


async def test_transfer_moves_ownership(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "handover", now=NOW)

    await sharing.transfer_ownership(uow, alice, folder.node.id, "bob", now=LATER)

    moved = await uow.nodes.get(folder.node.id)
    assert moved is not None and moved.owner_id == bob.id


async def test_transfer_moves_the_subtree(world: World) -> None:
    uow, alice, bob, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "handover", now=NOW)
    inner = await nodes().create_folder(uow, alice, folder.node.id, "inner", now=NOW)

    await sharing.transfer_ownership(uow, alice, folder.node.id, "bob", now=LATER)

    moved = await uow.nodes.get(inner.node.id)
    assert moved is not None and moved.owner_id == bob.id


async def test_transfer_moves_the_quota() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    store = FakeObjectStore()
    sharing = SharingService(FakeDirectory())
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "handover", now=NOW)
    await files(store).upload(uow, alice, folder.node.id, "a.bin", stream(PAYLOAD), now=NOW)

    await sharing.transfer_ownership(uow, alice, folder.node.id, "bob", now=LATER)

    alice_usage = await uow.quotas.get(alice.id)
    bob_usage = await uow.quotas.get(bob.id)
    assert alice_usage is not None and alice_usage.live_bytes == 0
    assert bob_usage is not None and bob_usage.live_bytes == len(PAYLOAD)


async def test_transfer_is_refused_when_the_recipient_lacks_room() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    bob.quota_bytes = 10
    store = FakeObjectStore()
    sharing = SharingService(FakeDirectory())
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "handover", now=NOW)
    await files(store).upload(uow, alice, folder.node.id, "a.bin", stream(PAYLOAD), now=NOW)

    with pytest.raises(QuotaExceededError):
        await sharing.transfer_ownership(uow, alice, folder.node.id, "bob", now=LATER)


async def test_the_previous_owner_keeps_editor_access(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "handover", now=NOW)

    await sharing.transfer_ownership(uow, alice, folder.node.id, "bob", now=LATER)

    assert (await nodes().get(uow, alice, folder.node.id)).role is Role.EDITOR


async def test_the_previous_owner_can_be_cut_off(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "handover", now=NOW)

    await sharing.transfer_ownership(
        uow, alice, folder.node.id, "bob", keep_editor_access=False, now=LATER
    )

    with pytest.raises(NotFoundError):
        await nodes().get(uow, alice, folder.node.id)


async def test_transfer_to_an_unknown_user_is_refused(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "handover", now=NOW)
    with pytest.raises(RecipientUnknownError):
        await sharing.transfer_ownership(uow, alice, folder.node.id, "stranger", now=LATER)


async def test_transfer_to_yourself_is_refused(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "handover", now=NOW)
    with pytest.raises(CannotShareWithSelfError):
        await sharing.transfer_ownership(uow, alice, folder.node.id, "alice", now=LATER)


async def test_transfer_is_audited(world: World) -> None:
    uow, alice, _, sharing = world
    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "handover", now=NOW)
    await sharing.transfer_ownership(uow, alice, folder.node.id, "bob", now=LATER)

    actions = [r.action for r in uow.audit.records]
    assert AuditAction.OWNERSHIP_TRANSFERRED in actions


# --- encryption guard ------------------------------------------------------


async def test_sharing_encrypted_content_without_a_key_service_fails_closed() -> None:
    """A share the recipient could never decrypt is worse than no share."""
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    await provision(uow, "bob")
    sharing = SharingService(FakeDirectory(), keys=None)

    folder = await nodes().create_folder(uow, alice, alice.root_folder_id, "secret", now=NOW)
    node = await uow.nodes.get(folder.node.id)
    assert node is not None
    node.encrypted = True  # as an encrypted file would be

    with pytest.raises(PermissionDeniedError, match="key service"):
        await sharing.grant(uow, alice, folder.node.id, "bob", Role.VIEWER, now=NOW)
