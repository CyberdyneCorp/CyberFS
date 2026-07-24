"""The S3 HTTP router -- dispatch, XML errors, and NotImplemented.

Wired over the in-memory unit of work and a stub authenticator so the transport
concerns are exercised without Postgres or MinIO: that a bearer-authenticated
caller reaches `ListBuckets`/`ListObjectsV2`, that every failure answers in the
S3 ``<Error>`` shape rather than problem+json (invariant D), and that an
unsupported operation is `NotImplemented`/501.
"""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree.ElementTree import fromstring

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.routers.s3 import create_s3_router
from cyberfs.adapters.inbound.api.s3.xml import S3_NAMESPACE
from cyberfs.application.content import ContentService
from cyberfs.application.nodes import NodeService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.application.s3_authentication import S3Authenticated
from cyberfs.application.s3_objects import S3ObjectService
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import S3AccessDeniedError
from cyberfs.domain.s3.credentials import S3Credential
from cyberfs.domain.s3.request import S3Request

from .fakes import FakeKeyProvider, FakeObjectStore, FakeUnitOfWork, stream

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
GB = 1024**3
ALICE = {"Authorization": "Bearer dev:alice"}


class StubAuthenticator:
    """Resolves any bearer request to Alice; a request with no token is denied."""

    async def authenticate(self, uow: object, request: S3Request) -> S3Authenticated:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            raise S3AccessDeniedError("a credential is required")
        return S3Authenticated(principal=Principal(subject="alice"), credential=S3Credential.BEARER)


@pytest.fixture
async def client() -> TestClient:
    uow = FakeUnitOfWork()
    store = FakeObjectStore()
    content = ContentService(
        store, max_upload_bytes=10 * GB, upload_chunk_bytes=8, version_retention_count=10
    )
    nodes = NodeService(max_tree_depth=64, page_size_max=1000)
    provisioning = ProvisioningService(FakeKeyProvider(), default_quota_bytes=10 * GB)

    alice = await provisioning.resolve(uow, Principal(subject="alice"), now=NOW)
    reports = await nodes.create_folder(uow, alice, alice.root_folder_id, "reports", now=NOW)
    await content.upload(uow, alice, reports.node.id, "q3.xlsx", stream(b"numbers"), now=NOW)

    app = FastAPI()
    app.state.unit_of_work = lambda: uow
    app.state.s3_authentication = StubAuthenticator()
    app.state.provisioning = provisioning
    app.state.s3_objects = S3ObjectService(nodes, content, page_size_max=1000)
    app.include_router(create_s3_router("/s3"))
    return TestClient(app, raise_server_exceptions=False)


def _code(body: bytes) -> str | None:
    element = fromstring(body).find(f"{{{S3_NAMESPACE}}}Code")  # noqa: S314 -- trusted server XML
    return element.text if element is not None else None


def test_list_buckets_returns_the_callers_bucket(client: TestClient) -> None:
    response = client.get("/s3/", headers=ALICE)
    assert response.status_code == 200
    assert "application/xml" in response.headers["content-type"]
    assert "<Name>alice</Name>" in response.text


def test_list_objects_v2_lists_the_tree(client: TestClient) -> None:
    response = client.get(
        "/s3/alice", params={"list-type": "2", "prefix": "reports/"}, headers=ALICE
    )
    assert response.status_code == 200
    assert "<Key>reports/q3.xlsx</Key>" in response.text


def test_a_missing_credential_is_access_denied_in_xml(client: TestClient) -> None:
    response = client.get("/s3/")
    assert response.status_code == 403
    assert "application/xml" in response.headers["content-type"]
    assert _code(response.content) == "AccessDenied"


def test_a_foreign_bucket_is_no_such_bucket(client: TestClient) -> None:
    response = client.get("/s3/bob", params={"list-type": "2"}, headers=ALICE)
    assert response.status_code == 404
    assert _code(response.content) == "NoSuchBucket"


def test_a_missing_object_is_no_such_key(client: TestClient) -> None:
    response = client.head("/s3/alice/reports/absent.txt", headers=ALICE)
    assert response.status_code == 404


def test_head_object_reports_size(client: TestClient) -> None:
    response = client.head("/s3/alice/reports/q3.xlsx", headers=ALICE)
    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(b"numbers"))


def test_put_object_stores_and_get_reads_it_back(client: TestClient) -> None:
    put = client.put("/s3/alice/reports/new.txt", content=b"data", headers=ALICE)
    assert put.status_code == 200
    assert put.headers["etag"]
    got = client.get("/s3/alice/reports/new.txt", headers=ALICE)
    assert got.content == b"data"


def test_delete_object_returns_no_content(client: TestClient) -> None:
    response = client.delete("/s3/alice/reports/q3.xlsx", headers=ALICE)
    assert response.status_code == 204
    # The object is gone from the listing afterwards.
    listing = client.get("/s3/alice", params={"list-type": "2"}, headers=ALICE)
    assert "q3.xlsx" not in listing.text


def test_copy_object_duplicates_via_the_copy_source_header(client: TestClient) -> None:
    response = client.put(
        "/s3/alice/reports/q3-copy.xlsx",
        headers={**ALICE, "x-amz-copy-source": "/alice/reports/q3.xlsx"},
    )
    assert response.status_code == 200
    assert "<CopyObjectResult" in response.text
    copied = client.get("/s3/alice/reports/q3-copy.xlsx", headers=ALICE)
    assert copied.content == b"numbers"


def test_delete_objects_batch_reports_each_key(client: TestClient) -> None:
    body = (
        "<Delete><Object><Key>reports/q3.xlsx</Key></Object>"
        "<Object><Key>reports/absent.txt</Key></Object></Delete>"
    )
    response = client.post("/s3/alice", params={"delete": ""}, content=body.encode(), headers=ALICE)
    assert response.status_code == 200
    assert "<DeleteResult" in response.text
    assert response.text.count("<Deleted>") == 2  # both idempotent successes


def test_an_unsupported_operation_is_not_implemented(client: TestClient) -> None:
    response = client.post("/s3/alice/reports/new.txt", content=b"data", headers=ALICE)
    assert response.status_code == 501
    assert _code(response.content) == "NotImplemented"


def test_a_subresource_operation_is_not_implemented(client: TestClient) -> None:
    response = client.get("/s3/alice", params={"acl": ""}, headers=ALICE)
    assert response.status_code == 501
    assert _code(response.content) == "NotImplemented"


def test_a_bad_max_keys_is_invalid_argument(client: TestClient) -> None:
    response = client.get("/s3/alice", params={"list-type": "2", "max-keys": "abc"}, headers=ALICE)
    assert response.status_code == 400
    assert _code(response.content) == "InvalidArgument"


def test_get_object_streams_bytes(client: TestClient) -> None:
    response = client.get("/s3/alice/reports/q3.xlsx", headers=ALICE)
    assert response.status_code == 200
    assert response.content == b"numbers"
