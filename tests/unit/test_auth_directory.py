"""Recipient lookup against CyberdyneAuth's org directory.

This adapter had no test of any kind. Every suite that touches sharing stubs the
`UserDirectory` port, and a stub cannot be missing an OAuth scope -- which is how
`503 the user directory is unavailable` reached production for every email
address, for weeks, while the real cause was a `403` from a dependency saying it
would not answer *this deployment*.

Driven through `httpx.MockTransport`, so the adapter's own HTTP handling is what
is under test rather than a network.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from cyberfs.adapters.outbound.auth.directory import CyberdyneDirectory, looks_like_a_subject
from cyberfs.domain.errors import DependencyForbiddenError, DependencyUnavailableError

BASE = "https://auth.example.test"
ORG = str(uuid.uuid4())
ALICE = "alice@example.test"


class StubTokens:
    """Stands in for `ServiceTokenProvider`; the token itself is not under test."""

    async def token(self) -> str:
        return "service-token"


def directory(handler: object) -> CyberdyneDirectory:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return CyberdyneDirectory(BASE, StubTokens(), httpx.AsyncClient(transport=transport))  # type: ignore[arg-type]


def members(*rows: dict[str, str]) -> httpx.Response:
    return httpx.Response(200, json={"members": list(rows)})


# --- a subject needs no lookup ---------------------------------------------


def test_a_uuid_is_already_a_subject() -> None:
    assert looks_like_a_subject(str(uuid.uuid4()))


def test_an_email_is_not_a_subject() -> None:
    assert not looks_like_a_subject(ALICE)


async def test_a_subject_shaped_recipient_makes_no_request() -> None:
    """The enumeration surface is the whole reason there is no global lookup."""
    subject = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError(f"a subject must not be looked up: {request.url}")

    assert await directory(handler).find_subject(subject, within_orgs=[ORG]) == subject


async def test_no_orgs_means_no_request_and_no_match() -> None:
    """Sharing by email resolves only within the sharer's own organisations."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("a lookup with no orgs must not reach the directory")

    assert await directory(handler).find_subject(ALICE, within_orgs=[]) is None


# --- the refusal that used to look like an outage --------------------------


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refusal_is_not_reported_as_an_outage(status: int) -> None:
    """The finding this file exists for.

    A `403 Insufficient scope` is a standing fact about how this deployment is
    registered. Reported as `dependency_unavailable` it reads as transient, and
    the reasonable response -- wait and retry -- can never succeed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"detail": "Insufficient scope: directory:read required"}
        )

    with pytest.raises(DependencyForbiddenError) as refused:
        await directory(handler).find_subject(ALICE, within_orgs=[ORG])

    assert refused.value.code == "dependency_forbidden"
    assert "directory:read" in str(refused.value), "the message must name the missing scope"


async def test_a_real_outage_is_still_an_outage() -> None:
    """The distinction cuts both ways, or it is just a renaming."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(DependencyUnavailableError):
        await directory(handler).find_subject(ALICE, within_orgs=[ORG])


async def test_a_server_error_is_an_outage_not_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(DependencyUnavailableError):
        await directory(handler).find_subject(ALICE, within_orgs=[ORG])


async def test_an_unknown_org_is_no_match_rather_than_an_error() -> None:
    """A 404 names the org, not the person: the caller simply has no such org."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert await directory(handler).find_subject(ALICE, within_orgs=[ORG]) is None


# --- resolution ------------------------------------------------------------


async def test_an_exact_address_resolves_to_its_subject() -> None:
    subject = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search"] == ALICE
        assert request.headers["Authorization"] == "Bearer service-token"
        return members({"id": subject, "email": ALICE})

    assert await directory(handler).find_subject(ALICE, within_orgs=[ORG]) == subject


async def test_a_near_miss_does_not_resolve() -> None:
    """`search` is a substring match, so the address is confirmed here.

    Without the exact comparison a share aimed at `bob@example.test` could land
    on `bobby@example.test`, which is a data disclosure rather than a typo.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return members({"id": str(uuid.uuid4()), "email": "alice.smith@example.test"})

    assert await directory(handler).find_subject(ALICE, within_orgs=[ORG]) is None


async def test_the_address_comparison_ignores_case() -> None:
    subject = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        return members({"id": subject, "email": "Alice@Example.TEST"})

    assert await directory(handler).find_subject(ALICE, within_orgs=[ORG]) == subject


async def test_several_orgs_are_searched_until_one_matches() -> None:
    subject = str(uuid.uuid4())
    second = str(uuid.uuid4())
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        org = str(request.url.path).rsplit("/", 2)[-2]
        seen.append(org)
        return members({"id": subject, "email": ALICE}) if org == second else members()

    assert await directory(handler).find_subject(ALICE, within_orgs=[ORG, second]) == subject
    assert seen == [ORG, second]


async def test_a_member_with_no_id_is_not_a_match() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return members({"email": ALICE})

    assert await directory(handler).find_subject(ALICE, within_orgs=[ORG]) is None


async def test_a_blank_recipient_resolves_to_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("a blank recipient must not reach the directory")

    assert await directory(handler).find_subject("   ", within_orgs=[ORG]) is None
