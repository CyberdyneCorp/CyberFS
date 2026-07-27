"""Partial label updates: the delta, the merge, and what a no-op costs.

What is worth testing here is the arithmetic and the refusals -- that the result
is the previous collection plus the additions minus the removals, that a limit is
checked against the merge rather than against the request, and that a delta which
changes nothing leaves no trace at all.

What is NOT testable here is the reason the endpoint exists: the fake models no
unique constraint, no advisory lock and no isolation, so `ON CONFLICT DO NOTHING`,
two disjoint patches surviving each other, the per-node maximum holding against
two patches at once, and two concurrent patches getting two distinct revisions all
live in tests/integration against real Postgres. The lock is pinned here only as a
call, never as serialization.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.audit import AuditAction
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
    ValidationError,
)
from cyberfs.domain.labels import (
    MetadataDelta,
    TagDelta,
    merge_metadata,
    merge_tags,
    validate_metadata_delta,
    validate_tag_delta,
)
from cyberfs.domain.nodes import (
    MAX_METADATA_PAIRS,
    MAX_TAG_LENGTH,
    MAX_TAGS_PER_NODE,
    RESERVED_METADATA_PREFIX,
)
from cyberfs.domain.sharing import Grant, Role
from cyberfs.domain.users import User
from cyberfs.infrastructure.settings import Environment

from .conftest import make_settings
from .fakes import FakeKeyProvider, FakeUnitOfWork

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3


async def provision(uow: FakeUnitOfWork, subject: str = "alice") -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


def service() -> NodeService:
    return NodeService(max_tree_depth=64, page_size_max=100)


async def a_folder(uow: FakeUnitOfWork, user: User, svc: NodeService, name: str = "docs"):
    return await svc.create_folder(uow, user, user.root_folder_id, name, now=NOW)


async def share(
    uow: FakeUnitOfWork, node_id: uuid.UUID, owner: User, recipient: User, role: Role
) -> None:
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=node_id,
            subject=recipient.subject,
            role=role,
            granted_by=owner.subject,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def revision_of(uow: FakeUnitOfWork, node_id: uuid.UUID) -> int:
    node = uow.nodes.by_id[node_id]
    return node.revision


# --- the delta, as a value -------------------------------------------------


def test_a_tag_delta_normalizes_both_directions() -> None:
    """The stored form is the only form, so a removal is folded like a write."""
    delta = validate_tag_delta([" Urgent "], ["DONE"])
    assert delta == TagDelta(added=frozenset({"urgent"}), removed=frozenset({"done"}))


def test_a_tag_named_in_both_directions_is_refused() -> None:
    with pytest.raises(ValidationError, match="both an addition and a removal"):
        validate_tag_delta(["urgent"], ["URGENT"])


def test_an_empty_tag_delta_is_refused() -> None:
    with pytest.raises(ValidationError, match="add or remove at least one"):
        validate_tag_delta([], [])


def test_a_blank_tag_is_refused_in_either_direction() -> None:
    with pytest.raises(ValidationError, match="empty or whitespace"):
        validate_tag_delta(["  "], [])
    with pytest.raises(ValidationError, match="empty or whitespace"):
        validate_tag_delta([], ["  "])


def test_an_over_long_tag_is_refused_in_either_direction() -> None:
    over = "x" * (MAX_TAG_LENGTH + 1)
    with pytest.raises(ValidationError, match=str(MAX_TAG_LENGTH)):
        validate_tag_delta([over], [])
    with pytest.raises(ValidationError, match=str(MAX_TAG_LENGTH)):
        validate_tag_delta([], [over])


def test_a_metadata_key_named_in_both_directions_is_refused() -> None:
    with pytest.raises(ValidationError, match="both a set and a removal"):
        validate_metadata_delta([("source", "sap")], ["source"])


def test_an_empty_metadata_delta_is_refused() -> None:
    with pytest.raises(ValidationError, match="set or remove at least one"):
        validate_metadata_delta([], [])


@pytest.mark.parametrize("key", [f"{RESERVED_METADATA_PREFIX}origin", "CyberFS.Origin"])
def test_a_reserved_key_is_refused_as_a_removal(key: str) -> None:
    """The guard has to see the deletion, which is why removals are named."""
    with pytest.raises(ValidationError, match="reserved"):
        validate_metadata_delta([], [key])


@pytest.mark.parametrize("key", [f"{RESERVED_METADATA_PREFIX}origin", "CyberFS.Origin"])
def test_a_reserved_key_is_refused_as_a_set(key: str) -> None:
    with pytest.raises(ValidationError, match="reserved"):
        validate_metadata_delta([(key, "v")], [])


def test_a_repeated_removal_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        validate_metadata_delta([], ["a", "a"])


# --- the merge, as a function ----------------------------------------------


def test_the_merge_is_previous_plus_added_minus_removed() -> None:
    delta = validate_tag_delta(["new"], ["stale"])
    assert merge_tags(frozenset({"keep", "stale"}), delta) == frozenset({"keep", "new"})


def test_the_merge_leaves_unnamed_metadata_keys_alone() -> None:
    delta = validate_metadata_delta([("b", "2")], ["c"])
    merged = merge_metadata({"a": "1", "b": "old", "c": "gone"}, delta)
    assert merged == {"a": "1", "b": "2"}


def test_a_small_delta_over_the_tag_limit_is_refused() -> None:
    """The request is one tag; the node is already full. The merge is what counts."""
    full = frozenset(f"tag-{i}" for i in range(MAX_TAGS_PER_NODE))
    with pytest.raises(ValidationError, match=str(MAX_TAGS_PER_NODE)):
        merge_tags(full, validate_tag_delta(["one-more"], []))


def test_a_small_delta_over_the_metadata_limit_is_refused() -> None:
    full = {f"k{i}": "v" for i in range(MAX_METADATA_PAIRS)}
    with pytest.raises(ValidationError, match=str(MAX_METADATA_PAIRS)):
        merge_metadata(full, validate_metadata_delta([("one-more", "v")], []))


def test_a_reserved_pair_counts_towards_the_metadata_limit() -> None:
    """It occupies a row like any other, and it is not the caller's to delete."""
    almost_full = {f"k{i}": "v" for i in range(MAX_METADATA_PAIRS - 1)}
    almost_full[f"{RESERVED_METADATA_PREFIX}origin"] = "system"
    with pytest.raises(ValidationError, match=str(MAX_METADATA_PAIRS)):
        merge_metadata(almost_full, validate_metadata_delta([("mine", "v")], []))


def test_a_reserved_pair_survives_a_merge_that_does_not_name_it() -> None:
    reserved = f"{RESERVED_METADATA_PREFIX}origin"
    merged = merge_metadata(
        {reserved: "system", "mine": "old"},
        MetadataDelta(pairs={"mine": "new"}, removed=frozenset()),
    )
    assert merged == {reserved: "system", "mine": "new"}


# --- use cases: tags -------------------------------------------------------


async def test_a_tag_delta_adds_and_removes_in_one_call() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_tags(uow, user, folder.node.id, ["keep", "stale"], now=NOW)

    _, resulting = await svc.patch_tags(
        uow, user, folder.node.id, add=["NEW"], remove=["stale"], now=LATER
    )

    assert resulting == frozenset({"keep", "new"})
    tags, _ = await svc.labels_for(uow, folder.node.id)
    assert tags == frozenset({"keep", "new"})


async def test_a_removal_written_in_another_case_removes_the_stored_tag() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_tags(uow, user, folder.node.id, ["Urgent"], now=NOW)

    _, resulting = await svc.patch_tags(uow, user, folder.node.id, remove=[" URGENT "], now=LATER)

    assert resulting == frozenset()


async def test_a_tag_delta_that_changes_something_bumps_the_revision_and_is_audited() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    before = revision_of(uow, folder.node.id)

    await svc.patch_tags(uow, user, folder.node.id, add=["confidential", "payroll"], now=LATER)

    assert revision_of(uow, folder.node.id) > before
    records = [r for r in uow.audit.records if r.action is AuditAction.NODE_TAGS_CHANGED]
    assert len(records) == 1
    assert records[0].context["added"] == 2
    assert records[0].context["removed"] == 0
    # Counts, not text: a label is not copied into a store pruned on another
    # clock, where it would outlive the label itself.
    rendered = str(records[0].context)
    assert "confidential" not in rendered
    assert "payroll" not in rendered


async def test_a_no_op_tag_delta_writes_nothing() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_tags(uow, user, folder.node.id, ["already"], now=NOW)
    before = revision_of(uow, folder.node.id)
    stable = (await svc.get(uow, user, folder.node.id)).etag
    audited = len(uow.audit.records)

    view, resulting = await svc.patch_tags(
        uow, user, folder.node.id, add=["ALREADY"], remove=["never-had-it"], now=LATER
    )

    assert resulting == frozenset({"already"})
    assert revision_of(uow, folder.node.id) == before
    assert len(uow.audit.records) == audited, "a change nobody can observe is not activity"
    assert view.etag == stable, "a no-op must not move the validator"


async def test_removing_a_tag_the_node_never_had_is_a_success() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    before = revision_of(uow, folder.node.id)

    _, resulting = await svc.patch_tags(uow, user, folder.node.id, remove=["ghost"], now=LATER)

    assert resulting == frozenset()
    assert revision_of(uow, folder.node.id) == before


async def test_a_tag_delta_over_the_limit_changes_nothing() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    full = [f"tag-{i}" for i in range(MAX_TAGS_PER_NODE)]
    await svc.replace_tags(uow, user, folder.node.id, full, now=NOW)
    before = revision_of(uow, folder.node.id)

    with pytest.raises(ValidationError, match=str(MAX_TAGS_PER_NODE)):
        await svc.patch_tags(uow, user, folder.node.id, add=["one-more"], now=LATER)

    tags, _ = await svc.labels_for(uow, folder.node.id)
    assert tags == frozenset(full)
    assert revision_of(uow, folder.node.id) == before


# --- use cases: metadata ---------------------------------------------------


async def test_a_metadata_delta_sets_and_deletes_leaving_the_rest_alone() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_metadata(
        uow, user, folder.node.id, [("keep", "  spaced  "), ("drop", "x"), ("bump", "old")], now=NOW
    )

    _, resulting = await svc.patch_metadata(
        uow, user, folder.node.id, pairs=[("bump", "new")], remove=["drop"], now=LATER
    )

    assert resulting == {"keep": "  spaced  ", "bump": "new"}, "unnamed keys byte-identical"


async def test_a_metadata_delta_records_what_it_wrote_and_dropped() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_metadata(uow, user, folder.node.id, [("gone", "x")], now=NOW)

    await svc.patch_metadata(
        uow, user, folder.node.id, pairs=[("added", "1")], remove=["gone"], now=LATER
    )

    records = [r for r in uow.audit.records if r.action is AuditAction.NODE_METADATA_CHANGED]
    assert records[-1].context["added"] == 1
    assert records[-1].context["removed"] == 1


async def test_setting_a_key_to_the_value_it_already_has_writes_nothing() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_metadata(uow, user, folder.node.id, [("source", "sap")], now=NOW)
    before = revision_of(uow, folder.node.id)
    audited = len(uow.audit.records)

    _, resulting = await svc.patch_metadata(
        uow, user, folder.node.id, pairs=[("source", "sap")], now=LATER
    )

    assert resulting == {"source": "sap"}
    assert revision_of(uow, folder.node.id) == before
    assert len(uow.audit.records) == audited


async def test_removing_a_key_the_node_does_not_have_is_a_success() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    before = revision_of(uow, folder.node.id)

    _, resulting = await svc.patch_metadata(uow, user, folder.node.id, remove=["ghost"], now=LATER)

    assert resulting == {}
    assert revision_of(uow, folder.node.id) == before


async def test_a_metadata_delta_over_the_limit_changes_nothing() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    full = [(f"k{i}", "v") for i in range(MAX_METADATA_PAIRS)]
    await svc.replace_metadata(uow, user, folder.node.id, full, now=NOW)
    before = revision_of(uow, folder.node.id)

    with pytest.raises(ValidationError, match=str(MAX_METADATA_PAIRS)):
        await svc.patch_metadata(uow, user, folder.node.id, pairs=[("one-more", "v")], now=LATER)

    _, metadata = await svc.labels_for(uow, folder.node.id)
    assert metadata == dict(full)
    assert revision_of(uow, folder.node.id) == before


# --- the reserved namespace ------------------------------------------------


async def test_a_reserved_pair_survives_a_replace_that_empties_the_collection() -> None:
    """A caller may not clear the namespace by replacing the part they can write.

    Written through the repository, which is how CyberFS itself would write one;
    the endpoint refuses such a key outright. Without this, the guard on a patch
    would be theatre -- a `PUT` of an empty list would do the same job.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    reserved = f"{RESERVED_METADATA_PREFIX}origin"
    await uow.nodes.set_metadata(folder.node.id, {reserved: "system", "mine": "x"})

    await svc.replace_metadata(uow, user, folder.node.id, [], now=LATER)

    assert await uow.nodes.metadata_for(folder.node.id) == {reserved: "system"}


async def test_a_reserved_pair_is_withheld_from_a_caller_but_not_from_cyberfs() -> None:
    """A key a caller can neither write nor remove is not one to hand it back.

    The repository read stays complete: that is how CyberFS reads its own
    namespace, and how a backup carries it.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    reserved = f"{RESERVED_METADATA_PREFIX}trusted"
    await uow.nodes.set_metadata(folder.node.id, {reserved: "yes", "mine": "x"})

    _, visible = await svc.labels_for(uow, folder.node.id)

    assert visible == {"mine": "x"}
    assert await uow.nodes.metadata_for(folder.node.id) == {reserved: "yes", "mine": "x"}


async def test_a_reserved_pair_is_absent_from_what_a_patch_returns() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await uow.nodes.set_metadata(folder.node.id, {f"{RESERVED_METADATA_PREFIX}trusted": "yes"})

    _, resulting = await svc.patch_metadata(
        uow, user, folder.node.id, pairs=[("mine", "x")], now=LATER
    )

    assert resulting == {"mine": "x"}


async def test_a_reserved_pair_still_makes_a_patch_a_no_op() -> None:
    """It is filtered out of a response, not out of the state being compared."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await uow.nodes.set_metadata(folder.node.id, {f"{RESERVED_METADATA_PREFIX}trusted": "yes"})
    before = revision_of(uow, folder.node.id)

    await svc.patch_metadata(uow, user, folder.node.id, remove=["absent"], now=LATER)

    assert revision_of(uow, folder.node.id) == before


# --- preconditions and authorization ---------------------------------------


@pytest.mark.parametrize("would_change", [True, False])
async def test_a_stale_precondition_is_refused_even_for_a_no_op(would_change: bool) -> None:
    """The token is a statement about the caller's view, not about the outcome."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    stale = folder.node.etag
    await svc.replace_tags(uow, user, folder.node.id, ["already"], now=NOW)
    before = revision_of(uow, folder.node.id)

    with pytest.raises(PreconditionFailedError):
        await svc.patch_tags(
            uow,
            user,
            folder.node.id,
            add=["fresh"] if would_change else ["ALREADY"],
            if_match=stale,
            now=LATER,
        )

    tags, _ = await svc.labels_for(uow, folder.node.id)
    assert tags == frozenset({"already"})
    assert revision_of(uow, folder.node.id) == before


async def test_a_current_precondition_is_honoured() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)

    _, resulting = await svc.patch_tags(
        uow, user, folder.node.id, add=["fresh"], if_match=folder.node.etag, now=LATER
    )

    assert resulting == frozenset({"fresh"})


async def test_a_viewer_cannot_patch_either_collection() -> None:
    uow = FakeUnitOfWork()
    owner = await provision(uow, "alice")
    reader = await provision(uow, "bob")
    svc = service()
    folder = await a_folder(uow, owner, svc)
    await share(uow, folder.node.id, owner, reader, Role.VIEWER)

    with pytest.raises(PermissionDeniedError):
        await svc.patch_tags(uow, reader, folder.node.id, add=["nope"], now=LATER)
    with pytest.raises(PermissionDeniedError):
        await svc.patch_metadata(uow, reader, folder.node.id, pairs=[("k", "v")], now=LATER)

    tags, metadata = await svc.labels_for(uow, folder.node.id)
    assert tags == frozenset()
    assert metadata == {}


async def test_an_editor_on_a_shared_node_may_patch() -> None:
    uow = FakeUnitOfWork()
    owner = await provision(uow, "alice")
    editor = await provision(uow, "bob")
    svc = service()
    folder = await a_folder(uow, owner, svc)
    await share(uow, folder.node.id, owner, editor, Role.EDITOR)

    _, resulting = await svc.patch_tags(uow, editor, folder.node.id, add=["shared"], now=LATER)

    assert resulting == frozenset({"shared"})


# --- the lock and the validator --------------------------------------------


@pytest.mark.parametrize("collection", ["tags", "metadata"])
async def test_a_patch_locks_the_node_before_reading_it(collection: str) -> None:
    """The fake's lock is a no-op, so this pins the call and its position only.

    Position is the load-bearing part: the limit check and the no-op judgement are
    made from a collection read after the lock, and a node read before it could be
    a pre-lock snapshot.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    node_id = folder.node.id

    events: list[str] = []
    reading = uow.nodes.get

    async def record_lock(target: uuid.UUID) -> None:
        events.append(f"lock:{target}")

    async def record_get(target: uuid.UUID):
        events.append(f"get:{target}")
        return await reading(target)

    uow.lock_subtree = record_lock  # type: ignore[method-assign]
    uow.nodes.get = record_get  # type: ignore[method-assign]

    if collection == "tags":
        await svc.patch_tags(uow, user, node_id, add=["locked"], now=LATER)
    else:
        await svc.patch_metadata(uow, user, node_id, pairs=[("k", "v")], now=LATER)

    assert events[0] == f"lock:{node_id}", "the lock must precede every read"
    assert f"get:{node_id}" in events[1:]


async def test_the_etag_a_patch_returns_is_the_one_a_following_read_reports() -> None:
    """A SQL revision increment would leave this on the pre-patch value."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    # Read off now: the fake hands out the same `Node` the service mutates.
    before = folder.etag

    patched, _ = await svc.patch_tags(uow, user, folder.node.id, add=["fresh"], now=LATER)

    fetched = await svc.get(uow, user, folder.node.id)
    assert patched.etag == fetched.etag
    assert patched.etag != before, "a real change must move the validator"


async def test_the_etag_a_patch_returns_is_accepted_as_the_next_precondition() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    patched, _ = await svc.patch_tags(uow, user, folder.node.id, add=["first"], now=LATER)

    _, resulting = await svc.patch_tags(
        uow, user, folder.node.id, add=["second"], if_match=patched.etag, now=LATER
    )

    assert resulting == frozenset({"first", "second"})


# --- the published contract ------------------------------------------------


def openapi() -> dict:
    """The schema only -- no lifespan, so no database is touched."""
    app = create_app(make_settings(environment=Environment.TEST))
    return dict(TestClient(app).get("/openapi.json").json())


@pytest.mark.parametrize("collection", ["tags", "metadata"])
def test_the_patch_route_is_published_alongside_the_replace(collection: str) -> None:
    """Both verbs on one path: a patch is an increment, a put is an assertion."""
    path = openapi()["paths"][f"/api/v1/nodes/{{node_id}}/{collection}"]

    assert set(path) >= {"put", "patch"}


def test_both_patch_bodies_are_published_with_their_two_directions() -> None:
    schemas = openapi()["components"]["schemas"]

    assert set(schemas["TagPatchRequest"]["properties"]) == {"add", "remove"}
    assert set(schemas["MetadataPatchRequest"]["properties"]) == {"set", "remove"}
    # The replace bodies are untouched: this change adds a verb, it does not
    # reshape the one that shipped.
    assert set(schemas["TagsRequest"]["properties"]) == {"tags"}
    assert set(schemas["MetadataRequest"]["properties"]) == {"metadata"}


async def test_a_trashed_node_cannot_be_patched() -> None:
    """The same refusal `rename` and `move` give, from the same authorization path.

    Labelling a trashed node has no effect a caller can observe -- search
    excludes it -- so consistency with the neighbouring mutations wins.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.delete(uow, user, folder.node.id, now=LATER)

    with pytest.raises(NotFoundError):
        await svc.patch_tags(uow, user, folder.node.id, add=["late"], now=LATER)
    with pytest.raises(NotFoundError):
        await svc.patch_metadata(uow, user, folder.node.id, pairs=[("k", "v")], now=LATER)
