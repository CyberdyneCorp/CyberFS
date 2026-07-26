"""Activity reporting -- `activity-reporting/spec.md`.

The pure rollup arithmetic, the self-scoped `ActivityService` (summary + feed,
window bound, action filter, owner-only names, purged nodes by id), and the
prune job that retains security records.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.activity import ActivityService
from cyberfs.application.jobs import ActivityPruneJob
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.activity import (
    ActivityCount,
    ActivityEntry,
    ActivityRollup,
    build_rollup,
)
from cyberfs.domain.audit import AuditAction, AuditProtocol, AuditRecord
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import ValidationError
from cyberfs.domain.nodes import Node, NodeKind
from cyberfs.domain.users import User
from cyberfs.infrastructure.settings import Settings

from .fakes import FakeKeyProvider, FakeUnitOfWork

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
#: The query instant `until` is exclusive, so recorded operations sit just
#: before it -- as they always do in reality, a record never being in the
#: future relative to the query.
EARLIER = NOW - timedelta(minutes=1)
GB = 1024**3


async def provision(uow: FakeUnitOfWork, subject: str = "alice") -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


def service(max_window: int = 90) -> ActivityService:
    return ActivityService(max_window_days=max_window, retention_days=90, page_size_max=1000)


def record(
    uow: FakeUnitOfWork,
    action: AuditAction,
    subject: str | None,
    *,
    target: str | None = None,
    byte_count: int | None = None,
    when: datetime = EARLIER,
    protocol: AuditProtocol = AuditProtocol.REST,
) -> None:
    context: dict[str, object] = {}
    if byte_count is not None:
        context["bytes"] = byte_count
    uow.audit.records.append(
        AuditRecord(
            action=action,
            occurred_at=when,
            actor_subject=subject,
            target_id=target,
            context=context,
            protocol=protocol,
        )
    )


def owned_node(owner: User, name: str = "notes.txt") -> Node:
    return Node(
        id=uuid.uuid4(),
        owner_id=owner.id,
        kind=NodeKind.FILE,
        name=name,
        parent_id=owner.root_folder_id,
        created_at=NOW,
        updated_at=NOW,
    )


# --- domain: build_rollup --------------------------------------------------


def test_build_rollup_counts_per_action() -> None:
    counts = [
        ActivityCount(AuditAction.FILE_UPLOADED, NOW.date(), count=2, bytes=30),
        ActivityCount(AuditAction.FILE_DOWNLOADED, NOW.date(), count=3, bytes=90),
        ActivityCount(AuditAction.GRANT_CREATED, NOW.date(), count=1),
        ActivityCount(AuditAction.GRANT_REVOKED, NOW.date(), count=1),
        ActivityCount(AuditAction.NODE_DELETED, NOW.date(), count=4),
        ActivityCount(AuditAction.NODE_RESTORED, NOW.date(), count=1),
        ActivityCount(AuditAction.VERSION_RESTORED, NOW.date(), count=2),
    ]
    rollup = build_rollup(counts, window_start=NOW - timedelta(days=1), window_end=NOW)

    assert rollup.uploads == 2
    assert rollup.downloads == 3
    assert rollup.shares_granted == 1
    assert rollup.shares_revoked == 1
    assert rollup.deletions == 4
    assert rollup.restores == 3  # node + version restores merge into one bucket


def test_build_rollup_counts_public_links_as_shares() -> None:
    """Regression: the summary used to contradict its own feed.

    Public links fed no counter, so a user who shared only by link saw
    `shares_granted: 0` next to a list of the links they had just issued.
    """
    counts = [
        ActivityCount(AuditAction.PUBLIC_LINK_CREATED, NOW.date(), count=6),
        ActivityCount(AuditAction.PUBLIC_LINK_REVOKED, NOW.date(), count=3),
    ]
    rollup = build_rollup(counts, window_start=NOW - timedelta(days=1), window_end=NOW)

    assert rollup.shares_granted == 6
    assert rollup.shares_revoked == 3


def test_build_rollup_merges_grants_and_links_into_share_totals() -> None:
    counts = [
        ActivityCount(AuditAction.GRANT_CREATED, NOW.date(), count=2),
        ActivityCount(AuditAction.PUBLIC_LINK_CREATED, NOW.date(), count=1),
        ActivityCount(AuditAction.GRANT_REVOKED, NOW.date(), count=1),
        ActivityCount(AuditAction.PUBLIC_LINK_REVOKED, NOW.date(), count=4),
    ]
    rollup = build_rollup(counts, window_start=NOW - timedelta(days=1), window_end=NOW)

    assert rollup.shares_granted == 3
    assert rollup.shares_revoked == 5


def test_build_rollup_byte_totals_are_upload_and_download_only() -> None:
    counts = [
        ActivityCount(AuditAction.FILE_UPLOADED, NOW.date(), count=1, bytes=100),
        ActivityCount(AuditAction.FILE_DOWNLOADED, NOW.date(), count=1, bytes=250),
        # A byte count on a non-transfer action must not leak into the totals.
        ActivityCount(AuditAction.NODE_COPIED, NOW.date(), count=1, bytes=999),
    ]
    rollup = build_rollup(counts, window_start=NOW, window_end=NOW)

    assert rollup.bytes_uploaded == 100
    assert rollup.bytes_downloaded == 250


def test_build_rollup_picks_the_busiest_day() -> None:
    quiet = NOW.date()
    busy = (NOW + timedelta(days=1)).date()
    counts = [
        ActivityCount(AuditAction.FILE_UPLOADED, quiet, count=1),
        ActivityCount(AuditAction.FILE_DOWNLOADED, busy, count=5),
        ActivityCount(AuditAction.NODE_CREATED, busy, count=2),
    ]
    rollup = build_rollup(counts, window_start=NOW, window_end=NOW)

    assert rollup.busiest_day == busy


def test_empty_rollup_is_all_zeroes() -> None:
    rollup = ActivityRollup.empty(NOW - timedelta(days=30), NOW)

    assert rollup.uploads == rollup.downloads == 0
    assert rollup.bytes_uploaded == rollup.bytes_downloaded == 0
    assert rollup.busiest_day is None


# --- service: summary ------------------------------------------------------


async def test_summary_totals_over_the_window() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)
    record(uow, AuditAction.FILE_UPLOADED, alice.subject, byte_count=10)
    record(uow, AuditAction.FILE_UPLOADED, alice.subject, byte_count=20)
    record(uow, AuditAction.FILE_DOWNLOADED, alice.subject, byte_count=5)
    record(uow, AuditAction.GRANT_CREATED, alice.subject)

    rollup, _ = await service().activity(uow, alice, window_days=30, limit=50, now=NOW)

    assert rollup.uploads == 2
    assert rollup.downloads == 1
    assert rollup.shares_granted == 1
    assert rollup.bytes_uploaded == 30
    assert rollup.bytes_downloaded == 5


async def test_a_quiet_account_returns_zeroes() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)

    rollup, feed = await service().activity(uow, alice, window_days=30, limit=50, now=NOW)

    assert rollup.uploads == 0
    assert rollup.busiest_day is None
    assert feed.items == ()


async def test_the_window_is_bounded() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)

    with pytest.raises(ValidationError):
        await service(max_window=90).activity(uow, alice, window_days=91, limit=50, now=NOW)


async def test_records_outside_the_window_are_excluded() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)
    record(uow, AuditAction.FILE_UPLOADED, alice.subject, byte_count=1, when=EARLIER)
    record(
        uow,
        AuditAction.FILE_UPLOADED,
        alice.subject,
        byte_count=1,
        when=NOW - timedelta(days=40),
    )

    rollup, _ = await service().activity(uow, alice, window_days=30, limit=50, now=NOW)

    assert rollup.uploads == 1


async def test_only_the_callers_own_operations_are_summarized() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    record(uow, AuditAction.FILE_UPLOADED, alice.subject, byte_count=1)
    record(uow, AuditAction.FILE_UPLOADED, bob.subject, byte_count=1)

    rollup, _ = await service().activity(uow, alice, window_days=30, limit=50, now=NOW)

    assert rollup.uploads == 1


async def test_a_recipient_download_counts_toward_their_own_activity() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow, "alice")
    bob = await provision(uow, "bob")
    alice_node = owned_node(alice, "secret.txt")
    await uow.nodes.add(alice_node)
    # Bob reads Alice's file: recorded against Bob, target is Alice's node.
    record(uow, AuditAction.FILE_DOWNLOADED, bob.subject, target=str(alice_node.id), byte_count=7)

    rollup, feed = await service().activity(uow, bob, window_days=30, limit=50, now=NOW)

    assert rollup.downloads == 1
    assert rollup.bytes_downloaded == 7
    # Bob does not own the node, so it is identified by id only.
    assert feed.items[0].node_id == str(alice_node.id)
    assert feed.items[0].node_name is None


# --- service: feed ---------------------------------------------------------


async def test_the_feed_is_newest_first() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)
    record(uow, AuditAction.FILE_UPLOADED, alice.subject, when=NOW - timedelta(hours=2))
    record(uow, AuditAction.FILE_DOWNLOADED, alice.subject, when=NOW - timedelta(hours=1))

    _, feed = await service().activity(uow, alice, window_days=30, limit=50, now=NOW)

    assert [entry.action for entry in feed.items] == [
        AuditAction.FILE_DOWNLOADED,
        AuditAction.FILE_UPLOADED,
    ]


async def test_the_feed_paginates_without_overlap() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)
    for index in range(5):
        record(
            uow,
            AuditAction.FILE_UPLOADED,
            alice.subject,
            when=NOW - timedelta(minutes=index),
        )

    _, first = await service().activity(uow, alice, window_days=30, limit=2, now=NOW)
    assert len(first.items) == 2
    assert first.next_cursor is not None

    _, second = await service().activity(
        uow, alice, window_days=30, limit=2, cursor=first.next_cursor, now=NOW
    )

    first_times = {entry.occurred_at for entry in first.items}
    second_times = {entry.occurred_at for entry in second.items}
    assert first_times.isdisjoint(second_times)
    assert len(second.items) == 2


async def test_the_feed_can_be_filtered_by_action() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)
    record(uow, AuditAction.FILE_UPLOADED, alice.subject, byte_count=10)
    record(uow, AuditAction.FILE_DOWNLOADED, alice.subject, byte_count=5)
    record(uow, AuditAction.FILE_DOWNLOADED, alice.subject, byte_count=5)

    rollup, feed = await service().activity(
        uow, alice, window_days=30, action=str(AuditAction.FILE_DOWNLOADED), limit=50, now=NOW
    )

    assert {entry.action for entry in feed.items} == {AuditAction.FILE_DOWNLOADED}
    assert len(feed.items) == 2
    # The summary stays unfiltered.
    assert rollup.uploads == 1
    assert rollup.downloads == 2


async def test_names_appear_only_for_owned_nodes() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)
    mine = owned_node(alice, "mine.txt")
    await uow.nodes.add(mine)
    record(uow, AuditAction.FILE_UPLOADED, alice.subject, target=str(mine.id), byte_count=1)

    _, feed = await service().activity(uow, alice, window_days=30, limit=50, now=NOW)

    assert feed.items[0].node_id == str(mine.id)
    assert feed.items[0].node_name == "mine.txt"


async def test_purged_nodes_remain_in_the_feed_by_id() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)
    gone = uuid.uuid4()  # a node id with no surviving row
    record(uow, AuditAction.NODE_DELETED, alice.subject, target=str(gone))

    _, feed = await service().activity(uow, alice, window_days=30, limit=50, now=NOW)

    assert feed.items[0].node_id == str(gone)
    assert feed.items[0].node_name is None


async def test_the_feed_reports_the_protocol() -> None:
    uow = FakeUnitOfWork()
    alice = await provision(uow)
    record(uow, AuditAction.FILE_UPLOADED, alice.subject, protocol=AuditProtocol.S3)

    _, feed = await service().activity(uow, alice, window_days=30, limit=50, now=NOW)

    assert feed.items[0].protocol is AuditProtocol.S3


# --- prune job -------------------------------------------------------------


def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://x/y",
        redis_url="redis://x",
        minio_endpoint="localhost:9000",
        minio_access_key="k",
        minio_secret_key="s",
        minio_bucket="b",
        cyberdyne_auth_base_url="https://auth.test",
        cyberfs_client_id="c",
        cyberfs_client_secret="s",
        master_key="ZGV2LW9ubHktbWFzdGVyLWtleS1kby1ub3QtdXNlISE=",
        activity_retention_days=30,
        _env_file=None,  # type: ignore[call-arg]
    )


async def test_prune_deletes_aged_activity_records() -> None:
    uow = FakeUnitOfWork()
    old = NOW - timedelta(days=40)
    record(uow, AuditAction.FILE_UPLOADED, "alice", when=old)
    record(uow, AuditAction.FILE_DOWNLOADED, "alice", when=NOW)

    result = await ActivityPruneJob(settings()).run(uow, now=NOW)

    assert result.records_pruned == 1
    remaining = {r.action for r in uow.audit.records}
    assert remaining == {AuditAction.FILE_DOWNLOADED}
    assert uow.committed == 1


async def test_prune_retains_security_records() -> None:
    uow = FakeUnitOfWork()
    old = NOW - timedelta(days=100)
    # Aged, but evidence: every one must survive the activity prune.
    for action in (
        AuditAction.GRANT_CREATED,
        AuditAction.AUTH_DENIED,
        AuditAction.ENCRYPTION_ENABLED,
        AuditAction.OWNERSHIP_TRANSFERRED,
        AuditAction.QUOTA_CHANGED,
    ):
        record(uow, action, "alice", when=old)
    record(uow, AuditAction.FILE_UPLOADED, "alice", when=old)

    result = await ActivityPruneJob(settings()).run(uow, now=NOW)

    assert result.records_pruned == 1  # only the upload
    survivors = {r.action for r in uow.audit.records}
    assert AuditAction.FILE_UPLOADED not in survivors
    assert {
        AuditAction.GRANT_CREATED,
        AuditAction.AUTH_DENIED,
        AuditAction.ENCRYPTION_ENABLED,
        AuditAction.OWNERSHIP_TRANSFERRED,
        AuditAction.QUOTA_CHANGED,
    } <= survivors


async def test_a_service_principal_is_refused() -> None:
    """`A service principal has no activity` -- refused before any resolution."""
    from cyberfs.adapters.inbound.api.routers import me as me_router
    from cyberfs.domain.errors import PermissionDeniedError

    uow = FakeUnitOfWork()
    principal = Principal(subject="client:robot", is_service=True)

    with pytest.raises(PermissionDeniedError):
        await me_router.activity(object(), principal, uow, window_days=30)  # type: ignore[arg-type]


def test_activity_entry_defaults_to_rest() -> None:
    entry = ActivityEntry(
        action=AuditAction.FILE_UPLOADED,
        occurred_at=NOW,
        node_id=None,
        node_name=None,
    )
    assert entry.protocol is AuditProtocol.REST
