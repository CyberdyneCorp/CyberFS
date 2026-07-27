"""Tags and key/value metadata: validation, authorization, and search.

The validation here is worth testing closely because it is the only thing
standing between a label set and unbounded row growth, and because refusing
loudly rather than silently dropping is what keeps a response honest about what
the caller asked for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.activity import ACTIVITY_ACTIONS, SECURITY_ACTIONS, SUMMARY_BUCKETS
from cyberfs.domain.audit import AuditAction
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
    ValidationError,
)
from cyberfs.domain.nodes import (
    MAX_METADATA_KEY_LENGTH,
    MAX_METADATA_PAIRS,
    MAX_METADATA_VALUE_LENGTH,
    MAX_TAG_LENGTH,
    MAX_TAGS_PER_NODE,
    RESERVED_METADATA_PREFIX,
    normalize_tag,
    validate_metadata,
    validate_tags,
)
from cyberfs.domain.sharing import Grant, Role
from cyberfs.domain.users import User

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


# --- tag validation --------------------------------------------------------


@pytest.mark.parametrize("raw", ["Urgent", " urgent ", "URGENT", "\turgent\n"])
def test_tags_normalize_to_one_form(raw: str) -> None:
    assert normalize_tag(raw) == "urgent"


def test_tags_are_a_set_so_duplicates_collapse() -> None:
    assert validate_tags(["a", "A", " a ", "b"]) == frozenset({"a", "b"})


def test_tag_order_does_not_matter() -> None:
    assert validate_tags(["b", "a"]) == validate_tags(["a", "b"])


@pytest.mark.parametrize("bad", ["", "   ", "\t", "\n"])
def test_an_empty_tag_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError, match="empty or whitespace"):
        validate_tags([bad])


def test_an_over_long_tag_is_refused() -> None:
    with pytest.raises(ValidationError, match=str(MAX_TAG_LENGTH)):
        validate_tags(["x" * (MAX_TAG_LENGTH + 1)])


def test_a_tag_at_the_limit_is_accepted() -> None:
    assert validate_tags(["x" * MAX_TAG_LENGTH]) == frozenset({"x" * MAX_TAG_LENGTH})


def test_too_many_tags_are_refused() -> None:
    with pytest.raises(ValidationError, match=str(MAX_TAGS_PER_NODE)):
        validate_tags([f"tag-{i}" for i in range(MAX_TAGS_PER_NODE + 1)])


# --- metadata validation ---------------------------------------------------


def test_metadata_round_trips() -> None:
    assert validate_metadata([("source", "sap"), ("id", "42")]) == {"source": "sap", "id": "42"}


def test_a_duplicate_key_is_refused_rather_than_deduplicated() -> None:
    """Silently keeping one value would leave the caller guessing which."""
    with pytest.raises(ValidationError, match="more than once"):
        validate_metadata([("a", "1"), ("a", "2")])


def test_an_empty_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="may not be empty"):
        validate_metadata([("", "v")])


def test_an_over_long_key_is_refused() -> None:
    with pytest.raises(ValidationError, match=str(MAX_METADATA_KEY_LENGTH)):
        validate_metadata([("k" * (MAX_METADATA_KEY_LENGTH + 1), "v")])


def test_an_over_long_value_is_refused() -> None:
    with pytest.raises(ValidationError, match=str(MAX_METADATA_VALUE_LENGTH)):
        validate_metadata([("k", "v" * (MAX_METADATA_VALUE_LENGTH + 1))])


def test_an_empty_value_is_allowed() -> None:
    """A key present with no value is a meaningful statement; a missing key is not."""
    assert validate_metadata([("flag", "")]) == {"flag": ""}


def test_too_many_pairs_are_refused() -> None:
    with pytest.raises(ValidationError, match=str(MAX_METADATA_PAIRS)):
        validate_metadata([(f"k{i}", "v") for i in range(MAX_METADATA_PAIRS + 1)])


@pytest.mark.parametrize("key", [f"{RESERVED_METADATA_PREFIX}origin", "CyberFS.Origin"])
def test_the_reserved_namespace_is_refused(key: str) -> None:
    """Case-insensitively, so the guard cannot be stepped around by shouting."""
    with pytest.raises(ValidationError, match="reserved"):
        validate_metadata([(key, "v")])


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    "action", [AuditAction.NODE_TAGS_CHANGED, AuditAction.NODE_METADATA_CHANGED]
)
def test_label_changes_are_activity_not_security_records(action: AuditAction) -> None:
    """Labelling is an ordinary operation, so it is pruned with other activity."""
    assert action in ACTIVITY_ACTIONS
    assert action not in SECURITY_ACTIONS
    assert action not in SUMMARY_BUCKETS


# --- use cases -------------------------------------------------------------


async def test_tags_are_stored_and_read_back() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)

    _, stored = await svc.replace_tags(uow, user, folder.node.id, ["Q3", "urgent"], now=LATER)

    assert stored == frozenset({"q3", "urgent"})
    tags, _ = await svc.labels_for(uow, folder.node.id)
    assert tags == frozenset({"q3", "urgent"})


async def test_replacing_tags_removes_the_previous_set() -> None:
    """`PUT` semantics: an empty list clears, which a merge could never express."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_tags(uow, user, folder.node.id, ["old"], now=LATER)

    await svc.replace_tags(uow, user, folder.node.id, [], now=LATER)

    tags, _ = await svc.labels_for(uow, folder.node.id)
    assert tags == frozenset()


async def test_metadata_is_stored_and_read_back() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)

    await svc.replace_metadata(uow, user, folder.node.id, [("source", "sap")], now=LATER)

    _, metadata = await svc.labels_for(uow, folder.node.id)
    assert metadata == {"source": "sap"}


async def test_changing_tags_bumps_the_revision() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    before = (await uow.nodes.get(folder.node.id)).revision  # type: ignore[union-attr]

    await svc.replace_tags(uow, user, folder.node.id, ["x"], now=LATER)

    after = (await uow.nodes.get(folder.node.id)).revision  # type: ignore[union-attr]
    assert after > before


async def test_a_stale_if_match_is_refused() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    stale = folder.node.etag
    await svc.replace_tags(uow, user, folder.node.id, ["first"], now=LATER)

    with pytest.raises(PreconditionFailedError):
        await svc.replace_tags(uow, user, folder.node.id, ["second"], if_match=stale, now=LATER)

    tags, _ = await svc.labels_for(uow, folder.node.id)
    assert tags == frozenset({"first"}), "a refused write must change nothing"


async def test_changing_tags_is_audited() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)

    await svc.replace_tags(uow, user, folder.node.id, ["x"], now=LATER)

    assert any(r.action is AuditAction.NODE_TAGS_CHANGED for r in uow.audit.records)


async def test_a_viewer_cannot_write_labels() -> None:
    uow = FakeUnitOfWork()
    owner = await provision(uow, "alice")
    reader = await provision(uow, "bob")
    svc = service()
    folder = await a_folder(uow, owner, svc)
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=folder.node.id,
            subject=reader.subject,
            role=Role.VIEWER,
            granted_by=owner.subject,
            created_at=NOW,
            updated_at=NOW,
        )
    )

    with pytest.raises(PermissionDeniedError):
        await svc.replace_tags(uow, reader, folder.node.id, ["nope"], now=LATER)
    with pytest.raises(PermissionDeniedError):
        await svc.replace_metadata(uow, reader, folder.node.id, [("k", "v")], now=LATER)


async def test_an_editor_may_write_labels() -> None:
    """Anyone trusted to change the content is trusted to describe it."""
    uow = FakeUnitOfWork()
    owner = await provision(uow, "alice")
    editor = await provision(uow, "bob")
    svc = service()
    folder = await a_folder(uow, owner, svc)
    await uow.grants.add(
        Grant(
            id=uuid.uuid4(),
            node_id=folder.node.id,
            subject=editor.subject,
            role=Role.EDITOR,
            granted_by=owner.subject,
            created_at=NOW,
            updated_at=NOW,
        )
    )

    _, stored = await svc.replace_tags(uow, editor, folder.node.id, ["shared"], now=LATER)
    assert stored == frozenset({"shared"})


async def test_a_stranger_cannot_see_or_write_labels() -> None:
    uow = FakeUnitOfWork()
    owner = await provision(uow, "alice")
    stranger = await provision(uow, "mallory")
    svc = service()
    folder = await a_folder(uow, owner, svc)

    with pytest.raises(NotFoundError):
        await svc.replace_tags(uow, stranger, folder.node.id, ["nope"], now=LATER)


async def test_a_copy_does_not_inherit_labels() -> None:
    """Matches how a copy already treats grants.

    `copy` deliberately carries no grants, because a copy belongs to the caller
    and may cross owners. Tags and metadata are assertions the *source* owner
    made, so inheriting them would import one user's labels into another user's
    namespace by way of a read.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    source = await a_folder(uow, user, svc, "source")
    destination = await a_folder(uow, user, svc, "destination")
    await svc.replace_tags(uow, user, source.node.id, ["private-label"], now=LATER)
    await svc.replace_metadata(uow, user, source.node.id, [("owner-note", "x")], now=LATER)

    copied = await svc.copy(uow, user, source.node.id, destination.node.id, now=LATER)

    tags, metadata = await svc.labels_for(uow, copied.node.id)
    assert tags == frozenset()
    assert metadata == {}
    # The original keeps its own.
    source_tags, _ = await svc.labels_for(uow, source.node.id)
    assert source_tags == frozenset({"private-label"})


# --- search ----------------------------------------------------------------


async def test_search_requires_at_least_one_filter() -> None:
    """An unfiltered search is a listing of everything reachable; walk the tree."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    with pytest.raises(ValidationError, match="name, a tag, or a metadata key"):
        await service().search(uow, user, "   ", limit=10)


async def test_a_metadata_value_without_its_key_is_refused() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    with pytest.raises(ValidationError, match="needs the key"):
        await service().search(uow, user, None, value="orphan", limit=10)


async def test_search_by_tag_finds_the_tagged_node() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    tagged = await a_folder(uow, user, svc, "tagged")
    await a_folder(uow, user, svc, "plain")
    await svc.replace_tags(uow, user, tagged.node.id, ["urgent"], now=LATER)

    found = await svc.search(uow, user, None, tags=["urgent"], limit=10)

    assert [n.id for n in found.items] == [tagged.node.id]


async def test_search_by_tag_is_case_insensitive() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_tags(uow, user, folder.node.id, ["Urgent"], now=LATER)

    assert len((await svc.search(uow, user, None, tags=["URGENT"], limit=10)).items) == 1


async def test_several_tags_narrow_rather_than_widen() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    both = await a_folder(uow, user, svc, "both")
    one = await a_folder(uow, user, svc, "one")
    await svc.replace_tags(uow, user, both.node.id, ["a", "b"], now=LATER)
    await svc.replace_tags(uow, user, one.node.id, ["a"], now=LATER)

    found = await svc.search(uow, user, None, tags=["a", "b"], limit=10)

    assert [n.id for n in found.items] == [both.node.id]


async def test_search_by_metadata_key_alone() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_metadata(uow, user, folder.node.id, [("source", "sap")], now=LATER)

    assert len((await svc.search(uow, user, None, key="source", limit=10)).items) == 1
    assert len((await svc.search(uow, user, None, key="other", limit=10)).items) == 0


async def test_search_by_metadata_key_and_value() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    folder = await a_folder(uow, user, svc)
    await svc.replace_metadata(uow, user, folder.node.id, [("source", "sap")], now=LATER)

    assert len((await svc.search(uow, user, None, key="source", value="sap", limit=10)).items) == 1
    assert (
        len((await svc.search(uow, user, None, key="source", value="other", limit=10)).items) == 0
    )


async def test_a_name_and_a_tag_narrow_together() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    match = await a_folder(uow, user, svc, "report-a")
    other = await a_folder(uow, user, svc, "report-b")
    await svc.replace_tags(uow, user, match.node.id, ["keep"], now=LATER)
    await svc.replace_tags(uow, user, other.node.id, ["drop"], now=LATER)

    found = await svc.search(uow, user, "report", tags=["keep"], limit=10)

    assert [n.id for n in found.items] == [match.node.id]
