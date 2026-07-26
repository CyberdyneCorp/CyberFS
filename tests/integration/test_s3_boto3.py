"""An unmodified S3 client round-trips against the S3 surface (task 6.9).

`boto3` lists and downloads a file over the S3-compatible surface and gets bytes
identical to what the REST surface returns for the same object. Skipped
gracefully when `boto3` is not installed or the live stack (Postgres + MinIO) is
unreachable, so `just test` stays usable without either.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from cyberfs.infrastructure.settings import Environment

from .conftest import build_settings

boto3 = pytest.importorskip("boto3")
from botocore.client import Config  # noqa: E402 -- guarded by importorskip above

pytestmark = pytest.mark.integration

ALICE = {"Authorization": "Bearer dev:alice"}
BOB = {"Authorization": "Bearer dev:bob"}
PAYLOAD = b"the quick brown fox jumps over the lazy dog\n" * 64
MINIO_ENDPOINT = "localhost:9000"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.contextmanager
def _serve(app: object, port: int) -> Iterator[None]:
    """Run the app under uvicorn in a background thread for the test's duration."""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")  # type: ignore[arg-type]
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("server did not start in time")
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def base_url(engine: AsyncEngine, session_factory: object) -> Iterator[str]:
    """A live CyberFS with the S3 surface enabled, or a skip when unreachable.

    Takes `session_factory` for its truncation, the same as every other module
    here. Without it rows survived between tests in this file while each test
    got a fresh app and a fresh bucket, so a user provisioned by an earlier test
    kept key material that no longer matched the objects around it.
    """
    from minio import Minio

    try:
        Minio(
            MINIO_ENDPOINT, access_key="cyberfs", secret_key="cyberfs-dev-secret", secure=False
        ).list_buckets()
    except Exception as exc:  # pragma: no cover - environment probe
        pytest.skip(f"no MinIO at {MINIO_ENDPOINT}: {type(exc).__name__}")

    from cyberfs.adapters.inbound.api.app import create_app

    settings = build_settings(
        auth_dev_mode=True,
        environment=Environment.TEST,
        minio_endpoint=MINIO_ENDPOINT,
        minio_access_key="cyberfs",
        minio_secret_key="cyberfs-dev-secret",
        minio_bucket=f"cyberfs-s3-{_free_port()}",
        minio_secure=False,
        s3_api_enabled=True,
        s3_region="us-east-1",
    )
    port = _free_port()
    with _serve(create_app(settings), port):
        yield f"http://127.0.0.1:{port}"


def _mint_key(base_url: str, who: dict[str, str] = ALICE) -> tuple[str, str]:
    response = httpx.post(f"{base_url}/api/v1/me/s3-keys", headers=who, json={"label": "test"})
    response.raise_for_status()
    body = response.json()
    return body["access_key_id"], body["secret_access_key"]


def _root(base_url: str, who: dict[str, str]) -> str:
    return str(httpx.get(f"{base_url}/api/v1/nodes/root", headers=who).json()["id"])


def _make_folder(base_url: str, who: dict[str, str], parent: str, name: str, **body: object) -> str:
    response = httpx.post(
        f"{base_url}/api/v1/nodes/{parent}/folders",
        headers=who,
        json={"name": name, **body},
    )
    response.raise_for_status()
    return str(response.json()["id"])


def _s3_client(base_url: str, access_key: str, secret: str) -> object:
    return boto3.client(
        "s3",
        endpoint_url=f"{base_url}/s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def test_boto3_lists_and_downloads_identically_to_rest(base_url: str) -> None:
    # Upload a file through REST.
    root = httpx.get(f"{base_url}/api/v1/nodes/root", headers=ALICE).json()["id"]
    upload = httpx.put(
        f"{base_url}/api/v1/nodes/{root}/files/report.txt",
        content=PAYLOAD,
        headers={**ALICE, "Content-Type": "text/plain"},
    )
    assert upload.status_code == 201, upload.text

    access_key, secret = _mint_key(base_url)
    client = _s3_client(base_url, access_key, secret)

    # The caller's single bucket is named for their subject.
    buckets = client.list_buckets()["Buckets"]
    assert [b["Name"] for b in buckets] == ["alice"]

    listing = client.list_objects_v2(Bucket="alice")
    assert "report.txt" in {item["Key"] for item in listing.get("Contents", [])}

    s3_bytes = client.get_object(Bucket="alice", Key="report.txt")["Body"].read()
    rest_bytes = httpx.get(f"{base_url}/api/v1/nodes/{root}/content", headers=ALICE).content
    assert s3_bytes == rest_bytes == PAYLOAD


def test_s3_upload_is_readable_through_rest_and_back(base_url: str) -> None:
    """Task 7.6: an S3 write is REST-readable and a REST write is S3-readable."""
    access_key, secret = _mint_key(base_url)
    client = _s3_client(base_url, access_key, secret)
    root = _root(base_url, ALICE)

    # S3 PutObject -> REST GetObject.
    client.put_object(Bucket="alice", Key="from-s3.txt", Body=PAYLOAD)
    node = _find_child(base_url, ALICE, root, "from-s3.txt")
    rest_bytes = httpx.get(f"{base_url}/api/v1/nodes/{node}/content", headers=ALICE).content
    assert rest_bytes == PAYLOAD

    # REST upload -> S3 GetObject.
    httpx.put(
        f"{base_url}/api/v1/nodes/{root}/files/from-rest.txt",
        content=PAYLOAD,
        headers={**ALICE, "Content-Type": "text/plain"},
    ).raise_for_status()
    s3_bytes = client.get_object(Bucket="alice", Key="from-rest.txt")["Body"].read()
    assert s3_bytes == PAYLOAD


def test_s3_upload_into_an_encrypted_folder_round_trips(base_url: str) -> None:
    """Task 7.6: encryption inheritance applies to an S3 upload and decrypts on read."""
    access_key, secret = _mint_key(base_url)
    client = _s3_client(base_url, access_key, secret)
    root = _root(base_url, ALICE)
    vault = _make_folder(base_url, ALICE, root, "vault", encryption_default="on")

    client.put_object(Bucket="alice", Key="vault/sealed.txt", Body=PAYLOAD)

    # The stored node inherited the folder's encryption default.
    node = _find_child(base_url, ALICE, vault, "sealed.txt")
    detail = httpx.get(f"{base_url}/api/v1/nodes/{node}", headers=ALICE).json()
    assert detail["encrypted"] is True

    # Both surfaces return the plaintext.
    s3_bytes = client.get_object(Bucket="alice", Key="vault/sealed.txt")["Body"].read()
    rest_bytes = httpx.get(f"{base_url}/api/v1/nodes/{node}/content", headers=ALICE).content
    assert s3_bytes == rest_bytes == PAYLOAD


def test_a_revoked_recipient_is_refused_on_the_next_s3_request(base_url: str) -> None:
    """Task 7.7: revocation takes effect on the recipient's very next S3 request."""
    from botocore.exceptions import ClientError

    _root(base_url, BOB)  # provision Bob
    root = _root(base_url, ALICE)
    team = _make_folder(base_url, ALICE, root, "team")
    httpx.put(
        f"{base_url}/api/v1/nodes/{team}/files/plan.txt", content=PAYLOAD, headers=ALICE
    ).raise_for_status()

    httpx.put(
        f"{base_url}/api/v1/nodes/{team}/grants",
        headers=ALICE,
        json={"recipient": "bob", "role": "viewer"},
    ).raise_for_status()

    access_key, secret = _mint_key(base_url, BOB)
    bob_client = _s3_client(base_url, access_key, secret)

    # While the grant stands, Bob reads the shared object through the reserved prefix.
    shared = bob_client.get_object(Bucket="bob", Key="shared/alice/team/plan.txt")["Body"].read()
    assert shared == PAYLOAD

    # Alice revokes; Bob's very next S3 request is refused, no cache-expiry wait.
    httpx.delete(f"{base_url}/api/v1/nodes/{team}/grants/bob", headers=ALICE).raise_for_status()
    with pytest.raises(ClientError) as denied:
        bob_client.get_object(Bucket="bob", Key="shared/alice/team/plan.txt")
    status = denied.value.response["ResponseMetadata"]["HTTPStatusCode"]
    assert status in (403, 404)


def test_a_multipart_upload_round_trips_byte_identically(base_url: str) -> None:
    """Task 8.6: a file large enough to force multipart round-trips identically.

    `aws-cli` is not installed, so boto3 drives the multipart upload directly: a
    small `multipart_threshold`/`multipart_chunksize` forces at least two parts,
    and the downloaded bytes must equal what was uploaded.
    """
    import io

    from boto3.s3.transfer import TransferConfig

    access_key, secret = _mint_key(base_url)
    client = _s3_client(base_url, access_key, secret)

    # A payload comfortably above the 5 MiB minimum part size, so a 5 MiB
    # chunk size splits it into several real parts.
    payload = b"cyberfs-multipart-" * (1024 * 1024)  # ~18 MiB
    config = TransferConfig(
        multipart_threshold=5 * 1024 * 1024,
        multipart_chunksize=5 * 1024 * 1024,
        max_concurrency=1,
    )
    client.upload_fileobj(io.BytesIO(payload), "alice", "large/blob.bin", Config=config)

    downloaded = client.get_object(Bucket="alice", Key="large/blob.bin")["Body"].read()
    assert downloaded == payload

    # It is equally readable through REST, proving the assembled object is a
    # normal versioned file, not a multipart special case.
    root = _root(base_url, ALICE)
    large = _find_child(base_url, ALICE, root, "large")
    node = _find_child(base_url, ALICE, large, "blob.bin")
    rest_bytes = httpx.get(f"{base_url}/api/v1/nodes/{node}/content", headers=ALICE).content
    assert rest_bytes == payload


def _find_child(base_url: str, who: dict[str, str], parent: str, name: str) -> str:
    page = httpx.get(f"{base_url}/api/v1/nodes/{parent}/children", headers=who).json()
    return next(str(item["id"]) for item in page["items"] if item["name"] == name)
