"""The WebDAV surface: XML, authentication, and the gates around it.

The properties worth pinning here are the ones a client depends on and the ones
that keep a default-on Basic surface from being a liability: the ETag agreeing
with REST, an unknown credential being indistinguishable from a wrong one, and
plaintext being refused in production.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.application.webdav_auth import WebDavAuthenticator, WebDavAuthError
from cyberfs.domain import webdav
from cyberfs.domain.nodes import Node, NodeKind
from cyberfs.domain.s3.access_key import S3AccessKey
from cyberfs.infrastructure.settings import Environment

from .conftest import make_settings
from .fakes import FakeKeyProvider, FakeUnitOfWork

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
DAV = "{DAV:}"


def a_node(name: str, *, folder: bool = False, size: int = 0, ct: str | None = None) -> Node:
    return Node(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        kind=NodeKind.FOLDER if folder else NodeKind.FILE,
        name=name,
        parent_id=uuid.uuid4(),
        created_at=NOW,
        updated_at=NOW,
        size_bytes=size,
        content_type=ct,
    )


def client(**overrides: object) -> TestClient:
    settings = make_settings(environment=Environment.TEST, **overrides)
    return TestClient(create_app(settings), raise_server_exceptions=False)


# --- XML -------------------------------------------------------------------


def test_a_collection_and_a_file_are_distinguishable() -> None:
    folder = a_node("papers", folder=True)
    document = a_node("notes.txt", size=12, ct="text/plain")

    body = webdav.multistatus("/webdav", [(folder, ""), (document, "notes.txt")])
    root = ElementTree.fromstring(body)  # noqa: S314 - our own output

    responses = root.findall(f"{DAV}response")
    assert len(responses) == 2
    types = [r.find(f".//{DAV}resourcetype") for r in responses]
    assert types[0] is not None and types[0].find(f"{DAV}collection") is not None
    assert types[1] is not None and types[1].find(f"{DAV}collection") is None


def test_a_file_reports_length_and_type_and_a_folder_does_not() -> None:
    """A folder has no content, so claiming 0 bytes would be a claim about content."""
    body = webdav.multistatus("/webdav", [(a_node("f.bin", size=42, ct="text/csv"), "f.bin")])
    assert "<D:getcontentlength>42</D:getcontentlength>" in body
    assert "text/csv" in body

    folder_body = webdav.multistatus("/webdav", [(a_node("d", folder=True), "d")])
    assert "getcontentlength" not in folder_body


def test_the_etag_is_the_rest_etag_verbatim() -> None:
    """A client caching on one surface must not be told the state has two tags."""
    node = a_node("shared.bin", size=5)
    body = webdav.multistatus("/webdav", [(node, "shared.bin")])
    assert f"<D:getetag>{node.etag}</D:getetag>" in body


def test_a_name_needing_escaping_survives() -> None:
    node = a_node("a & b <c>.txt", size=1)
    body = webdav.multistatus("/webdav", [(node, "a & b <c>.txt")])
    ElementTree.fromstring(body)  # noqa: S314 - must still parse
    assert "a &amp; b &lt;c&gt;.txt" in body


def test_an_href_is_percent_encoded_per_segment() -> None:
    assert webdav.href_for("/webdav", "a b/c&d.txt", is_collection=False) == (
        "/webdav/a%20b/c%26d.txt"
    )


def test_a_collection_href_ends_in_a_slash() -> None:
    """Several clients read the missing slash as "this is a file"."""
    assert webdav.href_for("/webdav", "docs", is_collection=True).endswith("/")


def test_an_error_is_dav_xml_not_the_rest_problem_document() -> None:
    body = webdav.error_body(404, "Not Found")
    root = ElementTree.fromstring(body)  # noqa: S314 - our own output
    assert root.tag == f"{DAV}error"


# --- authentication --------------------------------------------------------


class CountingKeyProvider(FakeKeyProvider):
    """Counts unseals, so the timing-equivalence property can be asserted without
    a wall clock. Mirrors the provider `test_s3_authenticator.py` uses for the
    same purpose; the counter lives here rather than on `FakeKeyProvider` so the
    two suites cannot double-count each other."""

    def __init__(self) -> None:
        super().__init__()
        self.unseal_calls = 0

    def unseal_secret(self, sealed: bytes, *, master_key_id: str) -> bytes:
        self.unseal_calls += 1
        return super().unseal_secret(sealed, master_key_id=master_key_id)


def basic(key_id: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{key_id}:{secret}".encode()).decode()


async def a_key(
    uow: FakeUnitOfWork, keys: FakeKeyProvider, subject: str = "alice"
) -> tuple[S3AccessKey, str]:
    """An active key with a known secret, built directly.

    Not via `S3AccessKeyService.mint`, which needs a provisioned `User` this test
    has no use for -- the authenticator only ever reads the stored key.
    """
    secret = "correct-horse-battery-staple"
    key = S3AccessKey(
        key_id="AKIATESTWEBDAV000001",
        sealed_secret=keys.seal_secret(secret.encode("utf-8")),
        secret_master_key_id=keys.master_key_id,
        label="test",
        owner_id=uuid.uuid4(),
        owner_subject=subject,
        created_at=NOW,
    )
    await uow.s3_keys.add(key)
    return key, secret


async def test_an_active_key_authenticates() -> None:
    uow, keys = FakeUnitOfWork(), FakeKeyProvider()
    key, secret = await a_key(uow, keys)

    authed = await WebDavAuthenticator(keys).authenticate(uow, basic(key.key_id, secret), now=NOW)
    assert authed.key_id == key.key_id
    assert authed.owner_subject == "alice"


@pytest.mark.parametrize("header", [None, "", "Bearer some-token", "Basic", "Basic !!!not-b64"])
async def test_a_non_basic_or_malformed_credential_is_refused(header: str | None) -> None:
    """A bearer token lands here too: this surface takes access keys and nothing else."""
    uow, keys = FakeUnitOfWork(), FakeKeyProvider()
    with pytest.raises(WebDavAuthError):
        await WebDavAuthenticator(keys).authenticate(uow, header, now=NOW)


async def test_an_unknown_key_is_refused() -> None:
    uow, keys = FakeUnitOfWork(), FakeKeyProvider()
    with pytest.raises(WebDavAuthError):
        await WebDavAuthenticator(keys).authenticate(uow, basic("AKIANOPE", "secret"), now=NOW)


async def test_a_wrong_secret_is_refused() -> None:
    uow, keys = FakeUnitOfWork(), FakeKeyProvider()
    key, _ = await a_key(uow, keys)
    with pytest.raises(WebDavAuthError):
        await WebDavAuthenticator(keys).authenticate(uow, basic(key.key_id, "wrong"), now=NOW)


async def test_a_revoked_key_stops_working() -> None:
    uow, keys = FakeUnitOfWork(), FakeKeyProvider()
    key, secret = await a_key(uow, keys)
    await uow.s3_keys.update(key.revoked(NOW))

    with pytest.raises(WebDavAuthError):
        await WebDavAuthenticator(keys).authenticate(uow, basic(key.key_id, secret), now=NOW)


async def test_an_unknown_key_does_the_same_unseal_work_as_a_real_one() -> None:
    """The timing side channel: without this, key ids become enumerable.

    Counting unseal calls rather than timing them -- a wall-clock assertion would
    be flaky, while the work itself is what has to happen.
    """
    uow, keys = FakeUnitOfWork(), CountingKeyProvider()
    key, _ = await a_key(uow, keys)
    authenticator = WebDavAuthenticator(keys)

    before = keys.unseal_calls
    with pytest.raises(WebDavAuthError):
        await authenticator.authenticate(uow, basic(key.key_id, "wrong"), now=NOW)
    known_cost = keys.unseal_calls - before

    before = keys.unseal_calls
    with pytest.raises(WebDavAuthError):
        await authenticator.authenticate(uow, basic("AKIAUNKNOWN", "wrong"), now=NOW)
    unknown_cost = keys.unseal_calls - before

    assert unknown_cost == known_cost


# --- the surface -----------------------------------------------------------


def test_options_advertises_class_1_and_the_implemented_methods() -> None:
    response = client().options("/webdav")
    assert response.status_code == 200
    assert response.headers["DAV"] == "1"
    advertised = {m.strip() for m in response.headers["Allow"].split(",")}
    assert advertised == set(webdav.ALLOWED_METHODS)
    assert "LOCK" not in advertised


@pytest.mark.parametrize("method", ["LOCK", "UNLOCK", "PROPPATCH"])
def test_unsupported_methods_are_refused_with_405(method: str) -> None:
    """405 rather than 404: the surface exists, it just will not do this."""
    assert client().request(method, "/webdav").status_code == 405


def test_a_missing_credential_is_challenged() -> None:
    response = client().get("/webdav")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic ")


def test_the_surface_is_mounted_with_no_configuration() -> None:
    """Pins the default. A later change must not flip it back silently."""
    assert client().options("/webdav").status_code == 200


def test_the_surface_can_be_switched_off() -> None:
    assert client(webdav_enabled=False).options("/webdav").status_code == 404


def test_the_routes_stay_out_of_the_openapi_schema() -> None:
    """WebDAV is not a JSON API; publishing it would mislead a generator."""
    schema = client().get("/openapi.json").json()
    assert not [p for p in schema["paths"] if "webdav" in p]


def test_plaintext_is_refused_in_production() -> None:
    """The load-bearing guard for a surface that is mounted by default."""
    settings = make_settings(environment=Environment.PRODUCTION)
    assert settings.webdav_requires_tls is True

    with TestClient(create_app(settings), raise_server_exceptions=False) as production:
        # TestClient speaks http, so this is exactly the refused case.
        assert production.get("/webdav").status_code == 403


def test_plaintext_is_allowed_in_local_development() -> None:
    for environment in (Environment.LOCAL, Environment.TEST):
        assert make_settings(environment=environment).webdav_requires_tls is False


def test_a_forwarded_https_header_satisfies_the_tls_requirement() -> None:
    """Production terminates TLS at the proxy, so the header is what we see."""
    settings = make_settings(environment=Environment.PRODUCTION)
    with TestClient(create_app(settings), raise_server_exceptions=False) as production:
        response = production.get("/webdav", headers={"X-Forwarded-Proto": "https"})
        # Past the TLS gate, so it fails on the credential instead.
        assert response.status_code == 401
