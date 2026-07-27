"""Paginated search and the tag inventory against real Postgres.

Everything here is something a fake cannot establish. The cursor predicate is
evaluated in SQL under the database's collation, so exhaustiveness and the
agreement between the cursor and the `ORDER BY` are only provable here. The
access scope is a join over grants that the fake node repository has no view of,
and the `node_tags` cascade needs foreign keys `FakeUnitOfWork` does not model.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings, minio_endpoint

pytestmark = pytest.mark.integration

ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
OCTET = "application/octet-stream"
ENDPOINT = minio_endpoint()
_unreachable: str | None = None


def _require_minio() -> None:
    """Skip rather than fail when the object store is not running locally."""
    global _unreachable
    if _unreachable is not None:
        pytest.skip(_unreachable)
    from minio import Minio

    try:
        Minio(
            ENDPOINT, access_key="cyberfs", secret_key="cyberfs-dev-secret", secure=False
        ).list_buckets()
    except Exception as exc:  # pragma: no cover - environment probe
        _unreachable = f"no MinIO at {ENDPOINT}: {type(exc).__name__}"
        pytest.skip(_unreachable)


def _settings(**overrides: object) -> object:
    return build_settings(
        auth_dev_mode=True,
        environment=Environment.TEST,
        minio_endpoint=ENDPOINT,
        minio_access_key="cyberfs",
        minio_secret_key="cyberfs-dev-secret",
        minio_bucket=f"cyberfs-search-{uuid.uuid4().hex[:8]}",
        minio_secure=False,
        **overrides,
    )


@pytest.fixture
def client(engine: object, session_factory: object) -> Iterator[TestClient]:
    _require_minio()
    with TestClient(create_app(_settings()), raise_server_exceptions=False) as test_client:  # type: ignore[arg-type]
        yield test_client


def root_id(client: TestClient, who: dict[str, str]) -> str:
    response = client.get("/api/v1/nodes/root", headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return str(response.json()["id"])


def folder(client: TestClient, who: dict[str, str], parent: str, name: str) -> str:
    response = client.post(f"/api/v1/nodes/{parent}/folders", json={"name": name}, headers=who)
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def upload(client: TestClient, who: dict[str, str], parent: str, name: str) -> str:
    response = client.put(
        f"/api/v1/nodes/{parent}/files/{name}",
        content=os.urandom(64),
        headers={**who, "Content-Type": OCTET},
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    return str(response.json()["id"])


def tag(client: TestClient, who: dict[str, str], node_id: str, *tags: str) -> None:
    response = client.put(f"/api/v1/nodes/{node_id}/tags", json={"tags": list(tags)}, headers=who)
    assert response.status_code == HTTPStatus.OK, response.text


def annotate(client: TestClient, who: dict[str, str], node_id: str, **pairs: str) -> None:
    response = client.put(
        f"/api/v1/nodes/{node_id}/metadata",
        json={"metadata": [{"key": k, "value": v} for k, v in pairs.items()]},
        headers=who,
    )
    assert response.status_code == HTTPStatus.OK, response.text


def page(
    client: TestClient, who: dict[str, str], path: str = "/api/v1/search", **params: object
) -> dict:
    response = client.get(path, params=params, headers=who)
    assert response.status_code == HTTPStatus.OK, response.text
    return dict(response.json())


def walk(
    client: TestClient, who: dict[str, str], path: str = "/api/v1/search", **params: object
) -> list[dict]:
    """Follow `next_cursor` to exhaustion, keeping page order and duplicates."""
    collected: list[dict] = []
    cursor: str | None = None
    for _ in range(50):  # A cursor that never clears is a bug, not a reason to hang.
        body = page(client, who, path, **params, **({"cursor": cursor} if cursor else {}))
        collected.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return collected
    raise AssertionError("the walk did not terminate")


def a_corpus(client: TestClient, who: dict[str, str], count: int, term: str = "item") -> set[str]:
    """`count` folders matching `term`, named in pure lowercase ASCII.

    Deliberately no digits or punctuation in the part that decides the order:
    how a collation weights those varies between clusters, and this suite is
    asserting the *order* rather than the alphabet.
    """
    root = root_id(client, who)
    return {
        folder(client, who, root, f"{chr(ord('a') + i // 26)}{chr(ord('a') + i % 26)}{term}")
        for i in range(count)
    }


# --- exhaustive walks ------------------------------------------------------


def test_a_name_search_returns_every_match_exactly_once(client: TestClient) -> None:
    created = a_corpus(client, ALICE, 12)

    found = [item["id"] for item in walk(client, ALICE, q="item", limit=5)]

    assert len(found) == len(created), "a page boundary dropped or repeated a match"
    assert set(found) == created


def test_matches_sharing_a_name_across_parents_are_each_returned_once(
    client: TestClient,
) -> None:
    """The case the missing identifier tie-break breaks.

    Names are unique only among siblings, so a cursor holding the name alone
    cannot say which `notes` the next page starts after.
    """
    root = root_id(client, ALICE)
    parents = [folder(client, ALICE, root, f"holder{chr(ord('a') + i)}") for i in range(4)]
    twins = {folder(client, ALICE, parent, "notes") for parent in parents}

    found = [item["id"] for item in walk(client, ALICE, q="notes", limit=1)]

    assert len(found) == len(twins)
    assert set(found) == twins
    assert [uuid.UUID(i) for i in found] == sorted(uuid.UUID(i) for i in found), (
        "ties are broken by identifier, ascending"
    )


def test_a_tag_search_and_a_metadata_search_both_walk_to_exhaustion(
    client: TestClient,
) -> None:
    created = a_corpus(client, ALICE, 7)
    for node in created:
        tag(client, ALICE, node, "bulk")
        annotate(client, ALICE, node, batch="q3")

    by_tag = [item["id"] for item in walk(client, ALICE, tag="bulk", limit=2)]
    by_key = [item["id"] for item in walk(client, ALICE, key="batch", value="q3", limit=3)]

    assert set(by_tag) == created and len(by_tag) == len(created)
    assert set(by_key) == created and len(by_key) == len(created)


def test_the_last_page_carries_no_cursor(client: TestClient) -> None:
    a_corpus(client, ALICE, 4)

    body = page(client, ALICE, q="item", limit=4)

    assert len(body["items"]) == 4
    assert body["next_cursor"] is None, "a caller must know the walk is over without asking again"


def test_the_cursor_walk_reproduces_the_unpaginated_order(client: TestClient) -> None:
    """The cursor's comparison and the `ORDER BY` are both evaluated by the
    database, and this is what proves they agree about "after"."""
    a_corpus(client, ALICE, 9)

    whole = [item["name"] for item in page(client, ALICE, q="item", limit=100)["items"]]
    paged = [item["name"] for item in walk(client, ALICE, q="item", limit=2)]

    assert paged == whole
    assert whole == sorted(whole), "normalized name ascending"


def test_folders_are_not_grouped_before_files(client: TestClient) -> None:
    """Unlike a folder listing. Pinned so a later "consistency" change argues here."""
    root = root_id(client, ALICE)
    upload(client, ALICE, root, "aaathing")
    folder(client, ALICE, root, "bbbthing")

    searched = [item["name"] for item in walk(client, ALICE, q="thing", limit=10)]
    listed = [
        item["name"] for item in page(client, ALICE, f"/api/v1/nodes/{root}/children")["items"]
    ]

    assert searched == ["aaathing", "bbbthing"], "name order, whatever the kind"
    assert listed[0] == "bbbthing", "the folder listing does rank the folder first"


# --- the cursor is bound to its filters ------------------------------------


@pytest.mark.parametrize(
    "other",
    [
        pytest.param({"q": "holder"}, id="a genuinely different result set"),
        pytest.param({"q": "item", "tag": "unrelated"}, id="the same term plus a tag filter"),
        pytest.param({"q": "item", "tag_match": "any"}, id="only the mode string differs"),
    ],
)
def test_a_cursor_presented_with_other_filters_is_refused(
    client: TestClient, other: dict[str, str]
) -> None:
    """The first case is the one that matters: a cursor from the `item` walk
    presented against a walk over different rows entirely. The mode-only variant
    is kept because the spec names it explicitly."""
    root = root_id(client, ALICE)
    a_corpus(client, ALICE, 4)
    folder(client, ALICE, root, "holderaa")
    issued = page(client, ALICE, q="item", limit=1)["next_cursor"]
    assert issued is not None

    response = client.get("/api/v1/search", params={**other, "cursor": issued}, headers=ALICE)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    assert "items" not in response.json()


def test_a_cursor_the_system_did_not_issue_is_refused(client: TestClient) -> None:
    a_corpus(client, ALICE, 2)

    response = client.get(
        "/api/v1/search", params={"q": "item", "cursor": "bm90LWEtY3Vyc29y"}, headers=ALICE
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text


# --- the access scope, across every page -----------------------------------


def test_a_walk_never_reaches_another_users_node(client: TestClient) -> None:
    hidden = a_corpus(client, ALICE, 6)
    root_id(client, BOB)

    assert walk(client, BOB, q="item", limit=2) == []
    assert hidden, "the fixture created nothing, so the assertion above is vacuous"


def test_an_active_grant_is_reachable_and_a_trashed_node_is_not(client: TestClient) -> None:
    root = root_id(client, ALICE)
    shared = folder(client, ALICE, root, "aashareditem")
    doomed = folder(client, ALICE, root, "abtrasheditem")
    for node in (shared, doomed):
        tag(client, ALICE, node, "scoped")
    root_id(client, BOB)
    granted = client.put(
        f"/api/v1/nodes/{shared}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert granted.status_code == HTTPStatus.CREATED, granted.text
    assert client.delete(f"/api/v1/nodes/{doomed}", headers=ALICE).status_code == HTTPStatus.OK

    for who in (ALICE, BOB):
        reachable = {item["id"] for item in walk(client, who, tag="scoped", limit=1)}
        assert doomed not in reachable, "a trashed node is not found, whoever asks"
    assert {item["id"] for item in walk(client, BOB, tag="scoped", limit=1)} == {shared}


def test_a_grant_makes_the_folder_findable_but_not_what_is_inside_it(
    client: TestClient,
) -> None:
    """Search is narrower than read access, by design rather than by accident.

    The scope predicate matches direct grant rows; read authorization resolves
    through an ancestor walk. So the recipient can `GET` the file inside the
    shared folder and cannot find it. Pinned here as a decision on the record --
    widening it is a non-goal, and this is the test a widening has to argue with.
    """
    root = root_id(client, ALICE)
    shared = folder(client, ALICE, root, "aasharednestitem")
    inside = folder(client, ALICE, shared, "abnesteditem")
    for node in (shared, inside):
        tag(client, ALICE, node, "nested")
    root_id(client, BOB)
    granted = client.put(
        f"/api/v1/nodes/{shared}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert granted.status_code == HTTPStatus.CREATED, granted.text

    readable = client.get(f"/api/v1/nodes/{inside}", headers=BOB)
    assert readable.status_code == HTTPStatus.OK, readable.text

    for params in ({"tag": "nested"}, {"q": "item"}):
        found = {item["id"] for item in walk(client, BOB, limit=1, **params)}
        assert found == {shared}, f"{params}: the granted node itself, and nothing under it"

    assert page(client, BOB, "/api/v1/tags")["items"] == [{"tag": "nested", "count": 1}], (
        "the inventory counts the granted node only, agreeing with the search"
    )


def test_a_tag_carried_only_below_a_shared_folder_is_absent_from_the_inventory(
    client: TestClient,
) -> None:
    """The aggregate and the search agree on the shared case, not merely on the
    owned one -- the scope predicate is one helper, and this is what shows it."""
    root = root_id(client, ALICE)
    shared = folder(client, ALICE, root, "aaouteritem")
    inside = folder(client, ALICE, shared, "abinneritem")
    tag(client, ALICE, shared, "outer")
    tag(client, ALICE, inside, "inneronly")
    root_id(client, BOB)
    granted = client.put(
        f"/api/v1/nodes/{shared}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert granted.status_code == HTTPStatus.CREATED, granted.text

    recipient = page(client, BOB, "/api/v1/tags")["items"]
    owner = page(client, ALICE, "/api/v1/tags")["items"]

    assert recipient == [{"tag": "outer", "count": 1}]
    assert owner == [{"tag": "inneronly", "count": 1}, {"tag": "outer", "count": 1}], (
        "the owner does see both, so the recipient's shorter list is a scope result"
    )


def test_a_pending_grant_stays_invisible_across_every_page(
    engine: object, session_factory: object
) -> None:
    """A share whose rewrap is unfinished confers nothing, pagination included.

    Built with the async-rewrap threshold at its floor, so a subtree holding more
    than one encrypted node is deferred and the grant is created pending.
    """
    _require_minio()
    with TestClient(
        create_app(_settings(async_rewrap_threshold_nodes=1)),  # type: ignore[arg-type]
        raise_server_exceptions=False,
    ) as client:
        root = root_id(client, ALICE)
        deferred = folder(client, ALICE, root, "aadeferreditem")
        for name in ("sealed-a.bin", "sealed-b.bin"):
            encrypted = client.put(
                f"/api/v1/nodes/{deferred}/files/{name}",
                content=os.urandom(512),
                params={"encrypted": "true"},
                headers={**ALICE, "Content-Type": OCTET},
            )
            assert encrypted.status_code == HTTPStatus.CREATED, encrypted.text
        tag(client, ALICE, deferred, "deferred")
        root_id(client, BOB)

        granted = client.put(
            f"/api/v1/nodes/{deferred}/grants",
            json={"recipient": "bob", "role": "viewer"},
            headers=ALICE,
        )
        assert granted.status_code == HTTPStatus.CREATED, granted.text
        listed = client.get(f"/api/v1/nodes/{deferred}/grants", headers=ALICE).json()["items"]
        assert listed, "no grant was created, so the assertions below prove nothing"
        # Still pending, so the node itself is unreachable -- which is what makes
        # its absence from the walk correct rather than coincidental.
        assert client.get(f"/api/v1/nodes/{deferred}", headers=BOB).status_code == (
            HTTPStatus.NOT_FOUND
        )

        assert walk(client, BOB, tag="deferred", limit=1) == []
        assert walk(client, BOB, path="/api/v1/tags", limit=1) == []


# --- the any-of tag mode ---------------------------------------------------


def test_the_any_of_mode_does_not_loosen_the_name_filter(client: TestClient) -> None:
    """The delta's flagship scenario, over real SQL and a real query string.

    One node matches the term and carries `draft`; the other carries `wip` and
    does not match the term. Both satisfy the any-of tag group, so a build that
    OR-ed the `ILIKE` into that group -- the exact ambiguity the spec says would
    make this parameter a liability -- returns both and fails here.
    """
    root = root_id(client, ALICE)
    matching = folder(client, ALICE, root, "aareportitem")
    other = folder(client, ALICE, root, "abmemoitem")
    tag(client, ALICE, matching, "draft")
    tag(client, ALICE, other, "wip")

    narrowed = walk(client, ALICE, q="report", tag=["draft", "wip"], tag_match="any", limit=1)
    unnarrowed = walk(client, ALICE, tag=["draft", "wip"], tag_match="any", limit=1)

    assert [item["id"] for item in narrowed] == [matching], "the term still narrows in any-of mode"
    assert {item["id"] for item in unnarrowed} == {matching, other}, (
        "without the term both nodes are in the union, so the assertion above is not vacuous"
    )


def test_the_any_of_mode_does_not_loosen_the_metadata_filter(client: TestClient) -> None:
    """The same rule for the other filter the mode must leave alone."""
    root = root_id(client, ALICE)
    keyed = folder(client, ALICE, root, "aakeyeditem")
    other = folder(client, ALICE, root, "abplainitem")
    tag(client, ALICE, keyed, "draft")
    tag(client, ALICE, other, "wip")
    annotate(client, ALICE, keyed, source="scanner")
    annotate(client, ALICE, other, source="camera")

    by_key = walk(client, ALICE, tag=["draft", "wip"], tag_match="any", key="source", limit=1)
    by_pair = walk(
        client, ALICE, tag=["draft", "wip"], tag_match="any", key="source", value="scanner", limit=1
    )

    assert {item["id"] for item in by_key} == {keyed, other}, "both carry the key"
    assert [item["id"] for item in by_pair] == [keyed], "the pinned value still narrows"


def test_an_undefined_tag_match_mode_is_refused_over_http(client: TestClient) -> None:
    """Where the refusal a caller can actually trigger lives: the route declares
    the enum, so an unknown spelling never reaches the domain."""
    a_corpus(client, ALICE, 2)

    response = client.get(
        "/api/v1/search", params={"q": "item", "tag_match": "anyy"}, headers=ALICE
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
    assert "items" not in response.json()


def test_any_of_returns_the_union_once_each_across_pages(client: TestClient) -> None:
    root = root_id(client, ALICE)
    drafts = {folder(client, ALICE, root, f"aa{chr(ord('a') + i)}draft") for i in range(4)}
    both = {folder(client, ALICE, root, f"bb{chr(ord('a') + i)}both") for i in range(3)}
    for node in drafts:
        tag(client, ALICE, node, "draft")
    for node in both:
        tag(client, ALICE, node, "draft", "wip")

    union = [
        item["id"] for item in walk(client, ALICE, tag=["draft", "wip"], tag_match="any", limit=2)
    ]
    every = [item["id"] for item in walk(client, ALICE, tag=["draft", "wip"], limit=2)]

    assert len(union) == len(drafts | both), "carrying both tags must not yield two rows"
    assert set(union) == drafts | both
    assert set(every) == both


# --- the tag inventory -----------------------------------------------------


def test_the_inventory_counts_only_what_the_caller_can_search(client: TestClient) -> None:
    root = root_id(client, ALICE)
    nodes = [folder(client, ALICE, root, f"aa{chr(ord('a') + i)}counted") for i in range(3)]
    for node in nodes:
        tag(client, ALICE, node, "counted")
    root_id(client, BOB)
    granted = client.put(
        f"/api/v1/nodes/{nodes[0]}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert granted.status_code == HTTPStatus.CREATED, granted.text

    assert page(client, ALICE, "/api/v1/tags")["items"] == [{"tag": "counted", "count": 3}]
    assert page(client, BOB, "/api/v1/tags")["items"] == [{"tag": "counted", "count": 1}], (
        "the count is per caller, not a property of the tag"
    )


def test_the_inventory_agrees_with_a_paginated_search(client: TestClient) -> None:
    created = a_corpus(client, ALICE, 9)
    for node in created:
        tag(client, ALICE, node, "inventoried")

    reported = page(client, ALICE, "/api/v1/tags")["items"]
    walked = walk(client, ALICE, tag="inventoried", limit=2)

    assert reported == [{"tag": "inventoried", "count": len(created)}]
    assert len(walked) == len(created), "a page boundary dropped or repeated a node"
    assert {item["id"] for item in walked} == created, (
        "a count alone would survive a walk that dropped one node and repeated another"
    )


def test_the_inventory_agrees_with_a_search_for_a_node_reached_by_grant(
    client: TestClient,
) -> None:
    """The case where two different scope predicates would actually disagree.

    Over owned nodes the inventory's aggregate and the search's `WHERE` agree by
    accident of both being right about ownership; it is the grant branch that
    catches a drift, so the agreement is asserted from the recipient's side.
    """
    root = root_id(client, ALICE)
    shared = [folder(client, ALICE, root, f"aa{chr(ord('a') + i)}granted") for i in range(3)]
    kept = folder(client, ALICE, root, "abaprivate")
    for node in [*shared, kept]:
        tag(client, ALICE, node, "crossscope")
    root_id(client, BOB)
    for node in shared:
        granted = client.put(
            f"/api/v1/nodes/{node}/grants",
            json={"recipient": "bob", "role": "viewer"},
            headers=ALICE,
        )
        assert granted.status_code == HTTPStatus.CREATED, granted.text

    reported = page(client, BOB, "/api/v1/tags")["items"]
    walked = walk(client, BOB, tag="crossscope", limit=2)

    assert reported == [{"tag": "crossscope", "count": len(shared)}]
    assert {item["id"] for item in walked} == set(shared)
    assert kept not in {item["id"] for item in walked}, "the owner's unshared node stayed out"


def test_trashing_the_last_carrier_removes_the_tag(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "aalonelyitem")
    tag(client, ALICE, node, "lonely")
    assert page(client, ALICE, "/api/v1/tags")["items"] == [{"tag": "lonely", "count": 1}]

    assert client.delete(f"/api/v1/nodes/{node}", headers=ALICE).status_code == HTTPStatus.OK

    assert page(client, ALICE, "/api/v1/tags")["items"] == [], "never a count of zero"


def test_purging_the_last_carrier_removes_the_tag(client: TestClient) -> None:
    """Exercises the `node_tags` foreign-key cascade, which no fake models."""
    node = folder(client, ALICE, root_id(client, ALICE), "aadoomeditem")
    tag(client, ALICE, node, "doomed")
    assert client.delete(f"/api/v1/nodes/{node}", headers=ALICE).status_code == HTTPStatus.OK

    purged = client.post(f"/api/v1/nodes/{node}/purge", headers=ALICE)
    assert purged.status_code == HTTPStatus.OK, purged.text

    assert page(client, ALICE, "/api/v1/tags")["items"] == []


def test_the_inventory_paginates_and_narrows_by_prefix(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "aavocabitem")
    vocabulary = [f"draft{chr(ord('a') + i)}" for i in range(5)] + ["final", "shipped"]
    tag(client, ALICE, node, *vocabulary)

    walked = [item["tag"] for item in walk(client, ALICE, path="/api/v1/tags", limit=2)]
    narrowed = [
        item["tag"] for item in walk(client, ALICE, path="/api/v1/tags", prefix="DRAFT", limit=2)
    ]

    assert walked == sorted(vocabulary), "each tag once, in tag order"
    assert narrowed == sorted(t for t in vocabulary if t.startswith("draft"))


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        pytest.param("%", ["%literal"], id="a percent is not every tag"),
        pytest.param("a_", ["a_underscore"], id="an underscore is not any character"),
    ],
)
def test_a_prefix_matches_pattern_characters_literally(
    client: TestClient, prefix: str, expected: list[str]
) -> None:
    """Only real SQL can catch a missing `_escape_like`.

    A fake matching with Python's `startswith` treats `%` literally whatever the
    query does, so the escaping is invisible to a unit test. Unescaped, `%` would
    return the whole vocabulary and `a_` would also match `ab`.
    """
    node = folder(client, ALICE, root_id(client, ALICE), "aapatternitem")
    tag(client, ALICE, node, "%literal", "a_underscore", "ab", "plain")

    narrowed = [item["tag"] for item in walk(client, ALICE, path="/api/v1/tags", prefix=prefix)]

    assert narrowed == expected


def test_a_limit_above_the_route_ceiling_is_refused(client: TestClient) -> None:
    """The outer half of the page bound, at the layer that owns it.

    The spec says an over-large limit is either refused or reduced and never
    served in full. On a default deployment `PAGE_SIZE_MAX` coincides with this
    ceiling, so the refusal is the half that is observable -- and it is FastAPI's,
    from the route's own declaration, before any use case runs. The clamp under
    the ceiling is asserted against the service in the unit suite.
    """
    a_corpus(client, ALICE, 2)

    for path in ("/api/v1/search", "/api/v1/tags"):
        response = client.get(path, params={"q": "item", "limit": 1001}, headers=ALICE)
        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text
        assert "items" not in response.json(), f"{path} returned results for an over-large limit"


def test_an_inventory_cursor_presented_with_another_prefix_is_refused(
    client: TestClient,
) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "aabounditem")
    tag(client, ALICE, node, "drafta", "draftb", "final")
    issued = page(client, ALICE, "/api/v1/tags", prefix="draft", limit=1)["next_cursor"]
    assert issued is not None

    response = client.get(
        "/api/v1/tags", params={"prefix": "final", "cursor": issued}, headers=ALICE
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, response.text


def test_the_inventory_requires_credentials(client: TestClient) -> None:
    node = folder(client, ALICE, root_id(client, ALICE), "aaprivateitem")
    tag(client, ALICE, node, "private-vocabulary")

    response = client.get("/api/v1/tags")

    assert response.status_code == HTTPStatus.UNAUTHORIZED, response.text
    assert "private-vocabulary" not in response.text


# --- what this change deliberately left alone ------------------------------


def test_shared_with_me_still_answers_without_a_cursor(client: TestClient) -> None:
    """`SearchResults` keeps its remaining caller: a `next_cursor` there would
    advertise pagination the route does not have."""
    shared = folder(client, ALICE, root_id(client, ALICE), "aashareditem")
    root_id(client, BOB)
    granted = client.put(
        f"/api/v1/nodes/{shared}/grants",
        json={"recipient": "bob", "role": "viewer"},
        headers=ALICE,
    )
    assert granted.status_code == HTTPStatus.CREATED, granted.text

    body = client.get("/api/v1/shared-with-me", headers=BOB)

    assert body.status_code == HTTPStatus.OK, body.text
    assert [item["id"] for item in body.json()["items"]] == [shared]
    assert "next_cursor" not in body.json()
