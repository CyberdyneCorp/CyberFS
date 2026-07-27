"""Paginated search and the tag inventory, at the use-case level.

What a fake can establish is the rules the use case owns: the filter set a cursor
is bound to, the clamp on the page size, what the tag match mode does and does
not loosen, and the shape of the inventory. Because the cursor codec and the
fingerprint are production code in `domain/pagination.py`, and because the use
case is what reads a cursor, these assertions land on real code rather than on a
copy the fake keeps.

Two things are deliberately out of reach here. The access scope -- the fake node
repository has no view of grants and `FakeUnitOfWork` models no foreign keys. And
whether the *SQL* walk is exhaustive: a walk that terminates against this fake
shows the use case threads the cursor correctly, not that the keyset predicate
Postgres evaluates under its own collation agrees with its `ORDER BY`. Both live
in `tests/integration/test_api_search.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI

from cyberfs.adapters.outbound.db.repositories import (
    SqlNodeRepository,
    _escape_like,
    decode_cursor,
    encode_cursor,
)
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import ValidationError
from cyberfs.domain.nodes import MAX_TAG_LENGTH, MAX_TAGS_PER_NODE
from cyberfs.domain.pagination import encode_keyed_cursor
from cyberfs.domain.ports.repositories import Page
from cyberfs.domain.search import SearchFilters, TagMatch
from cyberfs.domain.users import User

from .fakes import FakeKeyProvider, FakeUnitOfWork

NOW = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
GB = 1024**3


async def provision(uow: FakeUnitOfWork, subject: str = "alice") -> User:
    return await ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB).resolve(
        uow, Principal(subject=subject), now=NOW
    )


def service(page_size_max: int = 100) -> NodeService:
    return NodeService(max_tree_depth=64, page_size_max=page_size_max)


async def a_folder(
    uow: FakeUnitOfWork,
    user: User,
    svc: NodeService,
    name: str,
    *tags: str,
    metadata: dict[str, str] | None = None,
) -> None:
    created = await svc.create_folder(uow, user, user.root_folder_id, name, now=NOW)
    if tags:
        await svc.replace_tags(uow, user, created.node.id, tags, now=LATER)
    if metadata:
        await svc.replace_metadata(uow, user, created.node.id, list(metadata.items()), now=LATER)


async def walk(
    svc: NodeService, uow: FakeUnitOfWork, user: User, *, limit: int, **filters: object
) -> list[str]:
    """Follow `next_cursor` to exhaustion, collecting names in page order."""
    names: list[str] = []
    cursor: str | None = None
    for _ in range(20):  # A guard: a cursor that never clears is a bug, not a hang.
        page: Page = await svc.search(uow, user, limit=limit, cursor=cursor, **filters)  # type: ignore[arg-type]
        names.extend(node.name for node in page.items)
        cursor = page.next_cursor
        if cursor is None:
            return names
    raise AssertionError("the walk did not terminate")


# --- pagination ------------------------------------------------------------


async def test_the_use_case_threads_a_cursor_until_the_walk_terminates() -> None:
    """What a fake can show: the use case issues a cursor, reads its own cursor
    back, advances, and stops. That the *SQL* walk misses nothing is a different
    claim, made against Postgres in the integration suite.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    for i in range(5):
        await a_folder(uow, user, svc, f"report-{i}")

    assert await walk(svc, uow, user, term="report", limit=2) == [
        "report-0",
        "report-1",
        "report-2",
        "report-3",
        "report-4",
    ]


async def test_the_final_page_carries_no_cursor() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    await a_folder(uow, user, svc, "report-a")

    page = await svc.search(uow, user, "report", limit=10)

    assert [n.name for n in page.items] == ["report-a"]
    assert page.next_cursor is None
    assert not page.has_more


async def test_a_limit_above_the_maximum_is_clamped_and_still_pages() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service(page_size_max=2)
    for i in range(3):
        await a_folder(uow, user, svc, f"report-{i}")

    page = await svc.search(uow, user, "report", limit=1000)

    assert len(page.items) == 2, "the clamp bounds the page"
    assert page.next_cursor is not None, "the remainder is still reachable"
    assert await walk(svc, uow, user, term="report", limit=1000) == [
        "report-0",
        "report-1",
        "report-2",
    ]


# --- the cursor is bound to its filters ------------------------------------


async def issue_cursor(svc: NodeService, uow: FakeUnitOfWork, user: User, **filters: object) -> str:
    page = await svc.search(uow, user, limit=1, **filters)  # type: ignore[arg-type]
    assert page.next_cursor is not None, "the fixture did not produce a cursor"
    return page.next_cursor


@pytest.mark.parametrize(
    "other",
    [
        pytest.param({"term": "note"}, id="a different term"),
        pytest.param({"term": "report", "tags": ["urgent"]}, id="a different tag set"),
        pytest.param({"term": "report", "key": "source"}, id="a different metadata filter"),
        pytest.param(
            {"term": "report", "tags": ["a", "b"], "tag_match": TagMatch.ANY},
            id="a different tag mode",
        ),
    ],
)
async def test_a_cursor_from_another_filter_set_is_refused(other: dict[str, object]) -> None:
    """A page of a walk the cursor does not describe would be plausible nonsense."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    for name in ("report-a", "report-b", "note-a", "note-b"):
        await a_folder(uow, user, svc, name, "a", "b", "urgent")
    cursor = await issue_cursor(svc, uow, user, term="report")

    with pytest.raises(ValidationError, match="different filter set"):
        await svc.search(uow, user, limit=10, cursor=cursor, **other)  # type: ignore[arg-type]


@pytest.mark.parametrize("cursor", ["", "!!!not-base64!!!", "YWJj", "eA"])
async def test_a_cursor_the_system_did_not_issue_is_refused(cursor: str) -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    await a_folder(uow, user, svc, "report-a")

    with pytest.raises(ValidationError, match="cursor"):
        await svc.search(uow, user, "report", limit=10, cursor=cursor)


async def test_a_truncated_cursor_is_refused_rather_than_ignored() -> None:
    """Dropping an unreadable cursor would silently restart the walk."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    for i in range(3):
        await a_folder(uow, user, svc, f"report-{i}")
    cursor = await issue_cursor(svc, uow, user, term="report")

    with pytest.raises(ValidationError, match="cursor"):
        await svc.search(uow, user, "report", limit=10, cursor=cursor[:-4])


async def test_a_well_formed_cursor_naming_no_identifier_is_refused() -> None:
    """The check digest proves a payload was not truncated, not that its fields
    make sense. Anybody can compute one, so the sort key still has to be read as
    caller input -- and it is read in the use case, which is why this is provable
    without a database."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    filters = SearchFilters.of(term="report")
    forged = encode_cursor(encode_keyed_cursor(filters.fingerprint, "not-a-uuid", "report-a"))

    with pytest.raises(ValidationError, match="cursor is not valid"):
        await service().search(uow, user, "report", limit=10, cursor=forged)


async def test_a_cursor_without_a_filter_is_still_refused() -> None:
    """Otherwise pagination becomes a way to page through everything reachable."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    for i in range(3):
        await a_folder(uow, user, svc, f"report-{i}")
    cursor = await issue_cursor(svc, uow, user, term="report")

    with pytest.raises(ValidationError, match="name, a tag, or a metadata key"):
        await svc.search(uow, user, None, limit=10, cursor=cursor)


# --- the tag match mode ----------------------------------------------------


@pytest.mark.parametrize("mode", [TagMatch.ALL, TagMatch.ANY])
async def test_more_tags_than_a_node_may_carry_is_refused_in_either_mode(mode: TagMatch) -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)

    with pytest.raises(ValidationError, match=f"at most {MAX_TAGS_PER_NODE} tags"):
        await service().search(
            uow,
            user,
            None,
            tags=[f"t{i}" for i in range(MAX_TAGS_PER_NODE + 1)],
            tag_match=mode,
            limit=10,
        )


async def test_any_of_returns_a_node_carrying_one_tag_where_all_of_does_not() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    await a_folder(uow, user, svc, "one", "draft")
    await a_folder(uow, user, svc, "both", "draft", "wip")

    either = await svc.search(
        uow, user, None, tags=["draft", "wip"], tag_match=TagMatch.ANY, limit=10
    )
    every = await svc.search(uow, user, None, tags=["draft", "wip"], limit=10)

    assert [n.name for n in either.items] == ["both", "one"], "name order, and once each"
    assert [n.name for n in every.items] == ["both"]


@pytest.mark.parametrize("mode", [TagMatch.ALL, TagMatch.ANY])
def test_the_mode_leaves_the_name_and_metadata_filters_separately_conjoined(
    mode: TagMatch,
) -> None:
    """The flagship rule, asserted against the SQL the adapter actually builds.

    The two use-case tests below show the *behaviour* against an in-memory
    matcher, which cannot catch a mistake made in SQL. This one can: it compiles
    the real statement and requires the term, the tags, and the metadata pair to
    be three distinct operands of the top-level `AND`. OR-ing the name predicate
    into the any-of tag group -- the change the spec says would make this
    parameter a liability -- collapses two of those operands into one and fails
    here, with no database.
    """
    filters = SearchFilters.of(
        term="report", tags=["draft", "wip"], match=mode, key="source", value="scanner"
    )

    where = SqlNodeRepository.search_statement("alice", filters).whereclause
    assert where is not None
    operands = [
        str(clause.compile(compile_kwargs={"literal_binds": True})) for clause in where.clauses
    ]

    named = [sql for sql in operands if "lower(nodes.normalized_name) LIKE" in sql]
    tagged = [sql for sql in operands if "node_tags" in sql]
    keyed = [sql for sql in operands if "node_metadata" in sql]
    assert len(named) == 1, f"the name filter is not one top-level operand: {operands}"
    assert len(keyed) == 1, f"the metadata filter is not one top-level operand: {operands}"
    assert len(tagged) == (1 if mode is TagMatch.ANY else 2), (
        f"any-of is one EXISTS over an IN, all-of is one per tag: {tagged}"
    )
    assert not any("node_tags" in sql for sql in named + keyed), (
        "a tag predicate is fused with the name or the metadata filter, so the "
        f"mode can loosen something other than the tags: {operands}"
    )


async def test_the_any_of_mode_does_not_loosen_the_name_filter() -> None:
    """The same rule, as behaviour through the use case.

    The failure it describes is a plausible one: OR-ing the name predicate into
    the any-of group, so `?q=report&tag=a&tag=b&tag_match=any` would return
    everything tagged `b` whether or not it is a report. Both nodes here satisfy
    the tag group; only one satisfies the term.
    """
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    await a_folder(uow, user, svc, "report-a", "draft")
    await a_folder(uow, user, svc, "memo-b", "wip")

    page = await svc.search(
        uow, user, "report", tags=["draft", "wip"], tag_match=TagMatch.ANY, limit=10
    )

    assert [n.name for n in page.items] == ["report-a"], "the term still narrows in any-of mode"


async def test_the_any_of_mode_does_not_loosen_the_metadata_filter() -> None:
    """The same rule, for the other filter the mode must not touch."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    await a_folder(uow, user, svc, "keyed", "draft", metadata={"source": "scanner"})
    await a_folder(uow, user, svc, "unkeyed", "wip")
    await a_folder(uow, user, svc, "othervalue", "wip", metadata={"source": "camera"})

    by_key = await svc.search(
        uow, user, None, tags=["draft", "wip"], tag_match=TagMatch.ANY, key="source", limit=10
    )
    by_pair = await svc.search(
        uow,
        user,
        None,
        tags=["draft", "wip"],
        tag_match=TagMatch.ANY,
        key="source",
        value="scanner",
        limit=10,
    )

    assert [n.name for n in by_key.items] == ["keyed", "othervalue"], "the key still narrows"
    assert [n.name for n in by_pair.items] == ["keyed"], "the pinned value still narrows"


def test_an_unrecognized_tag_match_is_refused_by_the_domain() -> None:
    """Defense in depth, asserted where it lives.

    Over HTTP the route declares the enum, so Pydantic refuses `tag_match=anyy`
    with its own `422` before the domain sees a string (pinned in
    `tests/integration/test_api_search.py`). This guard is for the next caller
    holding a raw string -- a CLI, a job -- and `parse` is the only place a unit
    test can reach it now that the service takes the enum.
    """
    with pytest.raises(ValidationError, match="tag match mode must be one of: all, any"):
        TagMatch.parse("either")


async def test_a_tag_filter_longer_than_a_storable_tag_is_refused() -> None:
    """Not merely useless -- it cannot match, because the write path bounds a
    stored tag at the same length, so serving it would be a scan for nothing."""
    uow = FakeUnitOfWork()
    user = await provision(uow)

    with pytest.raises(ValidationError, match=f"at most {MAX_TAG_LENGTH} characters"):
        await service().search(uow, user, None, tags=["x" * (MAX_TAG_LENGTH + 1)], limit=10)

    fits = await service().search(uow, user, None, tags=["x" * MAX_TAG_LENGTH], limit=10)
    assert fits.items == (), "a tag at the bound is a legal filter that simply matches nothing"


# --- the tag inventory -----------------------------------------------------


async def test_the_inventory_reports_normalized_tags_with_counts_in_tag_order() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    await a_folder(uow, user, svc, "a", "Urgent", "draft")
    await a_folder(uow, user, svc, "b", " URGENT ")

    page = await svc.tag_inventory(uow, user, limit=10)

    assert [(u.tag, u.count) for u in page.items] == [("draft", 1), ("urgent", 2)]
    assert page.next_cursor is None


async def test_the_inventory_paginates() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    await a_folder(uow, user, svc, "a", "alpha", "beta", "gamma")

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        page = await svc.tag_inventory(uow, user, limit=2, cursor=cursor)
        seen.extend(u.tag for u in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == ["alpha", "beta", "gamma"]


async def test_a_tag_whose_last_carrier_is_trashed_leaves_the_inventory() -> None:
    """No tag is ever reported with a count of zero; the row simply stops existing."""
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    created = await svc.create_folder(uow, user, user.root_folder_id, "doomed", now=NOW)
    await svc.replace_tags(uow, user, created.node.id, ["gone"], now=LATER)

    await svc.delete(uow, user, created.node.id, now=LATER)

    assert (await svc.tag_inventory(uow, user, limit=10)).items == ()


async def test_the_inventory_prefix_matches_the_normalized_form() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    await a_folder(uow, user, svc, "a", "invoice-2026", "draft")

    for spelling in ("inv", "INV", "  Inv "):
        page = await svc.tag_inventory(uow, user, prefix=spelling, limit=10)
        assert [u.tag for u in page.items] == ["invoice-2026"], spelling


async def test_an_inventory_cursor_is_bound_to_its_prefix() -> None:
    uow = FakeUnitOfWork()
    user = await provision(uow)
    svc = service()
    await a_folder(uow, user, svc, "a", "draft-a", "draft-b", "final")

    page = await svc.tag_inventory(uow, user, prefix="draft", limit=1)
    assert page.next_cursor is not None

    with pytest.raises(ValidationError, match="different filter set"):
        await svc.tag_inventory(uow, user, prefix="final", limit=10, cursor=page.next_cursor)


# --- the adapter's share of the mechanism ----------------------------------


def test_the_opaque_codec_still_resolves_from_its_old_home() -> None:
    """The move to `domain/pagination.py` is a relocation, not a rewrite.

    `activity_queries`, the audit feed, the admin listings, and the trash listing
    all import these two names from the adapter. Covering the re-export means the
    move cannot silently break them.
    """
    assert decode_cursor(encode_cursor("a position")) == "a position"


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("100%", "100\\%"),
        ("a_b", "a\\_b"),
        ("back\\slash", "back\\\\slash"),
        ("plain", "plain"),
    ],
)
def test_pattern_characters_are_escaped_before_a_like(raw: str, escaped: str) -> None:
    """The helper the inventory's anchored `LIKE` depends on, which had no test.

    Whether the inventory *calls* it cannot be shown here: the fake matches a
    prefix with `startswith`, which treats `%` literally whatever the SQL does.
    That proof is an integration test.
    """
    assert _escape_like(raw) == escaped


# --- the wire contract -----------------------------------------------------


def test_the_openapi_schema_describes_the_page_and_the_inventory(app: FastAPI) -> None:
    """A generated client is how most callers reach these, so the schema is the
    contract rather than a by-product of it."""
    schema = app.openapi()

    search = schema["paths"]["/api/v1/search"]["get"]
    assert {p["name"] for p in search["parameters"]} >= {"q", "tag", "tag_match", "limit", "cursor"}
    assert search["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "NodePage"
    )

    inventory = schema["paths"]["/api/v1/tags"]["get"]
    assert {p["name"] for p in inventory["parameters"]} >= {"prefix", "limit", "cursor"}
    assert inventory["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "TagPage"
    )


def test_both_routes_declare_the_same_hard_page_ceiling(app: FastAPI) -> None:
    """Where the outer half of the page bound actually lives.

    The spec's bound has two layers: this static ceiling, which FastAPI refuses
    above (`422`, pinned over HTTP in the integration suite), and the configurable
    `PAGE_SIZE_MAX` clamp underneath it. Asserting the declaration here is what
    keeps "never served in full" true on a default deployment, where the clamp
    coincides with the ceiling and so cannot be observed.
    """
    schema = app.openapi()

    ceilings = {
        path: next(
            p["schema"]["maximum"]
            for p in schema["paths"][path]["get"]["parameters"]
            if p["name"] == "limit"
        )
        for path in ("/api/v1/search", "/api/v1/tags", "/api/v1/nodes/{node_id}/children")
    }

    assert set(ceilings.values()) == {1000}, f"the listings disagree about the ceiling: {ceilings}"
