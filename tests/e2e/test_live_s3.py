"""The S3-compatible surface against a deployed CyberFS.

Eleven operations with no coverage at this tier, and none of them reachable on
the current deployment: `S3_API_ENABLED` is unset, so `/s3` answers `404`. The
suite establishes that by observation and skips on it, rather than being silently
absent -- a capability nobody has ever exercised against a real deployment should
say so out loud.

Turn it on and these run. They are written against `boto3` deliberately: SigV4 is
the whole point of the surface, and a hand-rolled signature proves only that
CyberFS agrees with this file's idea of SigV4. A real client's signer is the thing
that has to be satisfied, including the parts easy to get wrong behind a proxy --
the canonical request's host header, and the payload hash of a streamed body.
"""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest

from .conftest import API_BASE_URL, requires_deployment

pytestmark = [pytest.mark.e2e, requires_deployment]


@pytest.fixture(scope="module")
def s3_enabled(anonymous: httpx.Client) -> bool:
    """Whether the deployment serves the S3 surface at all.

    Probed rather than read from configuration: this tier's job is to describe
    the deployment as it is, and `/s3` answering `404` is exactly how a disabled
    surface presents to a client.
    """
    return anonymous.get("/s3/").status_code != 404


@pytest.fixture(autouse=True)
def _needs_s3(s3_enabled: bool) -> None:
    if not s3_enabled:
        pytest.skip(
            "the deployment does not serve /s3 (S3_API_ENABLED is unset): set it to exercise "
            "the SigV4 surface, and note that no test has ever run against it on a real deployment"
        )


@pytest.fixture
def bucket(s3_key: tuple[str, str]):
    """A boto3 client bound to the deployment, signing with the access key."""
    boto3 = pytest.importorskip("boto3", reason="boto3 is needed to sign SigV4 requests")
    from botocore.config import Config

    key_id, secret = s3_key
    assert API_BASE_URL
    return boto3.client(
        "s3",
        endpoint_url=API_BASE_URL.rstrip("/") + "/s3",
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def test_the_surface_is_reachable_and_lists_buckets(bucket) -> None:
    """A signed `ListBuckets` -- the smallest thing that proves SigV4 agreed.

    Over a real ingress, because the canonical request includes the `Host`
    header: a proxy that rewrites it breaks every signature, and nothing
    in-process can show that.
    """
    response = bucket.list_buckets()
    assert "Buckets" in response, response


def test_an_object_round_trips_byte_for_byte(bucket, subject: str) -> None:
    body = os.urandom(4096)
    key = f"cyberfs-e2e-{uuid4().hex[:8]}.bin"

    bucket.put_object(Bucket=subject, Key=key, Body=body)
    try:
        fetched = bucket.get_object(Bucket=subject, Key=key)
        assert fetched["Body"].read() == body
    finally:
        bucket.delete_object(Bucket=subject, Key=key)


def test_a_head_reports_the_size(bucket, subject: str) -> None:
    body = os.urandom(1024)
    key = f"cyberfs-e2e-{uuid4().hex[:8]}.bin"
    bucket.put_object(Bucket=subject, Key=key, Body=body)
    try:
        assert bucket.head_object(Bucket=subject, Key=key)["ContentLength"] == len(body)
    finally:
        bucket.delete_object(Bucket=subject, Key=key)


def test_a_wrong_secret_is_refused(subject: str, s3_key: tuple[str, str]) -> None:
    """The signature has to actually be checked, not merely parsed."""
    boto3 = pytest.importorskip("boto3")
    from botocore.config import Config
    from botocore.exceptions import ClientError

    key_id, _ = s3_key
    assert API_BASE_URL
    impostor = boto3.client(
        "s3",
        endpoint_url=API_BASE_URL.rstrip("/") + "/s3",
        aws_access_key_id=key_id,
        aws_secret_access_key="not-the-secret",
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    with pytest.raises(ClientError) as refused:
        impostor.list_objects_v2(Bucket=subject)
    assert refused.value.response["ResponseMetadata"]["HTTPStatusCode"] in (401, 403)


def test_an_object_written_over_s3_is_visible_over_rest(
    bucket, api: httpx.Client, subject: str, root_id: str
) -> None:
    """One filesystem, three surfaces: the invariant that makes this safe."""
    body = os.urandom(2048)
    key = f"cyberfs-e2e-{uuid4().hex[:8]}.bin"
    bucket.put_object(Bucket=subject, Key=key, Body=body)

    try:
        listing = api.get(f"/api/v1/nodes/{root_id}/children", params={"limit": 200})
        node = next(item for item in listing.json()["items"] if item["name"] == key)
        assert api.get(f"/api/v1/nodes/{node['id']}/content").content == body
    finally:
        bucket.delete_object(Bucket=subject, Key=key)


def test_another_users_bucket_is_refused(bucket) -> None:
    """A bucket is a user's namespace, so naming someone else's must not work."""
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError) as refused:
        bucket.list_objects_v2(Bucket=str(uuid4()))
    assert refused.value.response["ResponseMetadata"]["HTTPStatusCode"] in (403, 404)
