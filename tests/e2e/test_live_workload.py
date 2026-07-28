"""A real corpus on the deployment, because it has never carried one.

`docs/outstanding-verification.md` records that production holds one user and
almost no data, so every claim about behaviour at scale is untested: pagination
that never needed a second page, a search whose index has never been the
difference between a scan and a lookup, a trash listing whose page aggregate has
only ever summed one row.

This tier seeds a corpus large enough for those to mean something, walks the
surfaces that page, and reports timings. It is deliberately **not** a benchmark:
numbers from a shared deployment over the public internet are not a measurement of
the database. What it does establish is that the paging contracts hold when there
is more than one page, and that a full walk stays within a budget a human would
accept — the failure mode being a query that degrades superlinearly and only shows
up past a few hundred rows.

Opt-in, because it writes hundreds of nodes to a live deployment:

    CYBERFS_LIVE_WORKLOAD=1 uv run pytest tests/e2e/test_live_workload.py -m e2e

Everything is created inside the run's own scratch folder and purged afterwards.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import httpx
import pytest

from .conftest import requires_deployment

pytestmark = [pytest.mark.e2e, requires_deployment]

#: Enough that a default page (100) is exceeded several times over, and enough
#: for an index to matter, while still seeding in a tolerable number of requests.
CORPUS = int(os.environ.get("CYBERFS_LIVE_WORKLOAD_NODES", "240"))

#: A full paginated walk of the corpus, end to end. Generous on purpose: this is
#: a public-internet round trip per page, so the budget catches "quadratic" rather
#: than "slow".
WALK_BUDGET_SECONDS = 90.0

#: The tag every seeded node carries, so one filter selects the whole corpus.
BULK_TAG = "workload"


def _enabled() -> bool:
    return os.environ.get("CYBERFS_LIVE_WORKLOAD") == "1"


pytestmark.append(
    pytest.mark.skipif(
        not _enabled(),
        reason=(
            f"set CYBERFS_LIVE_WORKLOAD=1 to seed ~{CORPUS} nodes on the deployment; "
            "production has never carried a corpus, so nothing here has been proved at scale"
        ),
    )
)


@pytest.fixture(scope="module")
def corpus(api: httpx.Client, scratch: str) -> Iterator[list[str]]:
    """`CORPUS` files under one folder, each tagged and annotated.

    Module-scoped: seeding is the expensive part, and every test here reads the
    same corpus rather than building its own.
    """
    holder = api.post(f"/api/v1/nodes/{scratch}/folders", json={"name": "workload"})
    assert holder.status_code == 201, holder.text
    parent = str(holder.json()["id"])

    ids: list[str] = []
    started = time.monotonic()
    for index in range(CORPUS):
        # Names are zero-padded so the keyset order is also lexicographic, which
        # makes a dropped or repeated page obvious when one is diagnosing.
        name = f"item-{index:04d}.bin"
        created = api.put(
            f"/api/v1/nodes/{parent}/files/{name}",
            content=f"payload-{index}".encode(),
            headers={"Content-Type": "application/octet-stream"},
        )
        assert created.status_code == 201, created.text
        node_id = str(created.json()["id"])
        ids.append(node_id)
        api.put(f"/api/v1/nodes/{node_id}/tags", json={"tags": [BULK_TAG, f"bucket-{index % 8}"]})
        api.put(
            f"/api/v1/nodes/{node_id}/metadata",
            json={"metadata": [{"key": "batch", "value": str(index % 8)}]},
        )
    elapsed = time.monotonic() - started
    print(f"\nseeded {CORPUS} nodes in {elapsed:.1f}s ({elapsed / CORPUS * 1000:.0f} ms/node)")

    yield ids

    api.delete(f"/api/v1/nodes/{parent}")
    api.post(f"/api/v1/nodes/{parent}/purge")


def walk(api: httpx.Client, path: str, **params: object) -> tuple[list[dict], float, int]:
    """Follow every cursor to exhaustion. Returns items, seconds, and page count."""
    collected: list[dict] = []
    cursor: str | None = None
    pages = 0
    started = time.monotonic()
    while True:
        query = {**params, **({"cursor": cursor} if cursor else {})}
        response = api.get(path, params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        collected.extend(body["items"])
        pages += 1
        cursor = body.get("next_cursor")
        if not cursor:
            break
        assert pages < 200, "cursor did not terminate"
    return collected, time.monotonic() - started, pages


# --- the paging contracts, with more than one page -------------------------


def test_a_tag_search_returns_every_seeded_node_exactly_once(
    api: httpx.Client, corpus: list[str]
) -> None:
    """Set equality, not a count: a walk that drops one node and repeats another
    has the right length and the wrong contents."""
    items, seconds, pages = walk(api, "/api/v1/search", tag=BULK_TAG, limit=50)
    print(f"tag search: {len(items)} items over {pages} pages in {seconds:.1f}s")

    assert pages > 1, "the corpus must exceed one page or this proves nothing"
    assert {item["id"] for item in items} == set(corpus)
    assert seconds < WALK_BUDGET_SECONDS, f"walk took {seconds:.1f}s"


def test_a_name_search_paginates_over_the_corpus(api: httpx.Client, corpus: list[str]) -> None:
    items, seconds, pages = walk(api, "/api/v1/search", q="item-", limit=50)
    print(f"name search: {len(items)} items over {pages} pages in {seconds:.1f}s")

    assert {item["id"] for item in items} >= set(corpus)
    assert seconds < WALK_BUDGET_SECONDS, f"walk took {seconds:.1f}s"


def test_the_ordering_is_stable_across_two_independent_walks(
    api: httpx.Client, corpus: list[str]
) -> None:
    """A cursor is only meaningful if the order is deterministic. Two walks of an
    unchanged corpus must agree exactly, or paging is quietly lossy."""
    first, _, _ = walk(api, "/api/v1/search", tag=BULK_TAG, limit=37)
    second, _, _ = walk(api, "/api/v1/search", tag=BULK_TAG, limit=53)

    assert [item["id"] for item in first] == [item["id"] for item in second], (
        "the same corpus walked with two page sizes returned two different orders"
    )


def test_no_page_is_short_while_more_remain(api: httpx.Client, corpus: list[str]) -> None:
    """A short page with a cursor is how a filter applied after `LIMIT` presents."""
    cursor: str | None = None
    while True:
        params: dict[str, object] = {"tag": BULK_TAG, "limit": 25}
        if cursor:
            params["cursor"] = cursor
        body = api.get("/api/v1/search", params=params).json()
        cursor = body.get("next_cursor")
        if cursor:
            assert len(body["items"]) == 25, f"short page of {len(body['items'])} with more to come"
        else:
            break


def test_the_tag_inventory_counts_the_whole_corpus(api: httpx.Client, corpus: list[str]) -> None:
    """The aggregate over a real corpus, not over one row."""
    rows, seconds, pages = walk(api, "/api/v1/tags", prefix=BULK_TAG, limit=50)
    print(f"tag inventory: {len(rows)} tags over {pages} pages in {seconds:.1f}s")

    bulk = next(row for row in rows if row["tag"] == BULK_TAG)
    assert bulk["count"] == len(corpus), bulk


def test_a_metadata_search_narrows_to_its_bucket(api: httpx.Client, corpus: list[str]) -> None:
    """One eighth of the corpus, which only means something when the corpus is
    large enough for a wrong answer to be visible."""
    items, seconds, _ = walk(api, "/api/v1/search", key="batch", value="3", limit=50)
    print(f"metadata search: {len(items)} items in {seconds:.1f}s")

    assert items, "the metadata filter matched nothing"
    assert len(items) == len([i for i in range(CORPUS) if i % 8 == 3])


# --- the trash at scale ----------------------------------------------------


def test_the_trash_lists_a_large_deletion_as_one_entry_with_real_totals(
    api: httpx.Client, scratch: str
) -> None:
    """The page aggregate and the subtree totals, over a subtree worth summing.

    Kept separate from the shared corpus because it deletes what it creates.
    """
    holder = api.post(f"/api/v1/nodes/{scratch}/folders", json={"name": "bulk-trash"}).json()["id"]
    total = 0
    for index in range(60):
        body = f"trash-{index}".encode()
        total += len(body)
        api.put(
            f"/api/v1/nodes/{holder}/files/t-{index:03d}.bin",
            content=body,
            headers={"Content-Type": "application/octet-stream"},
        )

    assert api.delete(f"/api/v1/nodes/{holder}").status_code in (200, 204)

    started = time.monotonic()
    listing = api.get("/api/v1/trash", params={"limit": 50})
    seconds = time.monotonic() - started
    assert listing.status_code == 200, listing.text
    entry = next(e for e in listing.json()["items"] if e["id"] == holder)
    print(f"trash listing with a 61-node entry: {seconds:.2f}s")

    assert entry["node_count"] == 61, entry
    assert entry["size_bytes"] == total, entry
    assert seconds < 10.0, f"a single trash page took {seconds:.1f}s"

    assert api.post(f"/api/v1/nodes/{holder}/purge").status_code == 200


# --- what the corpus says about quota accounting ---------------------------


def test_the_reported_usage_matches_what_was_written(
    api: httpx.Client, corpus: list[str], is_admin: bool
) -> None:
    """Quota accounting over hundreds of writes rather than one.

    Counter drift is cumulative, so it is invisible at one file and obvious at a
    few hundred. Read through the admin surface because that is where usage is
    reported; skipped when the account cannot see it.
    """
    if not is_admin:
        pytest.skip("needs the admin surface to read reported usage")

    from .conftest import subject_of

    token = api.headers["Authorization"].removeprefix("Bearer ").strip()
    me = next(
        row
        for row in api.get("/api/v1/admin/users", params={"limit": 200}).json()["items"]
        if row["subject"] == subject_of(token)
    )

    assert me["live_bytes"] >= 0 and me["used_bytes"] >= me["live_bytes"], me
    assert me["file_count"] >= len(corpus), (
        f"reported {me['file_count']} files, seeded at least {len(corpus)}"
    )
