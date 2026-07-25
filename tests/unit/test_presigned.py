"""Presigned URL SigV4 -- the pure query form.

Proves generation, parsing, and the round trip line up, that the canonical
query excludes only ``X-Amz-Signature``, and that a generated URL addresses the
CyberFS endpoint and carries no object-store host or credential. The
signature-verification round trip closes the loop: what
:mod:`cyberfs.domain.s3.presigned` signs, recomputing over the same canonical
request reproduces exactly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from cyberfs.application.s3_presign import S3PresignService
from cyberfs.domain.errors import InvalidArgumentError
from cyberfs.domain.s3 import presigned, sigv4
from cyberfs.domain.s3.access_key import S3AccessKey

from .fakes import FakeKeyProvider

ENDPOINT = "https://s3.cyberfs.example"
BASE_PATH = "/s3"
BUCKET = "user-1"
KEY = "reports/q3.xlsx"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
ACCESS_KEY_ID = "AKIATEST0000000000CI"
REGION = "us-east-1"
SERVICE = "s3"
AMZ_DATE = "20260724T120000Z"

# A MinIO host that must never surface in a generated URL.
MINIO_HOST = "minio.internal:9000"


def _generate(*, method: str = "GET", expires: int = 900) -> str:
    path = presigned.object_path(BASE_PATH, BUCKET, KEY)
    query = presigned.build_presigned_query(
        method=method,
        path=path,
        host=urlsplit(ENDPOINT).netloc,
        access_key_id=ACCESS_KEY_ID,
        secret=SECRET,
        region=REGION,
        service=SERVICE,
        amz_date=AMZ_DATE,
        expires=expires,
    )
    return presigned.presigned_url(ENDPOINT, path, query)


# --- generation addresses CyberFS, never the object store ------------------


def test_presigned_url_addresses_the_cyberfs_endpoint() -> None:
    url = _generate()
    assert url.startswith(f"{ENDPOINT}{BASE_PATH}/{BUCKET}/")
    assert urlsplit(url).netloc == urlsplit(ENDPOINT).netloc


def test_presigned_url_carries_no_object_store_host_or_credential() -> None:
    url = _generate().lower()
    assert MINIO_HOST.lower() not in url
    assert "minio" not in url
    # The only credential is the CyberFS access-key id, never a MinIO key.
    assert ACCESS_KEY_ID.lower() in url
    assert SECRET.lower() not in url  # the secret is never in the URL


def test_generated_query_has_the_six_amz_parameters() -> None:
    url = _generate()
    params = parse_qs(urlsplit(url).query)
    assert params["X-Amz-Algorithm"] == [sigv4.ALGORITHM]
    assert params["X-Amz-Credential"][0].startswith(f"{ACCESS_KEY_ID}/")
    assert params["X-Amz-Date"] == [AMZ_DATE]
    assert params["X-Amz-Expires"] == ["900"]
    assert params["X-Amz-SignedHeaders"] == ["host"]
    assert params["X-Amz-Signature"][0]


# --- canonical query drops only the signature ------------------------------


def test_canonical_query_excludes_only_the_signature() -> None:
    query = (
        "X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKID%2F20260724%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20260724T120000Z"
        "&X-Amz-Expires=900"
        "&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=deadbeef"
    )
    canonical = presigned.canonical_query_without_signature(query)
    assert "X-Amz-Signature" not in canonical
    assert "deadbeef" not in canonical
    # Every other parameter survives, sorted by name.
    assert canonical.startswith("X-Amz-Algorithm=AWS4-HMAC-SHA256")
    assert "X-Amz-SignedHeaders=host" in canonical


def test_canonical_query_is_sorted_and_percent_encoded() -> None:
    canonical = presigned.canonical_query_without_signature(
        "X-Amz-Date=20260724T120000Z&X-Amz-Algorithm=AWS4-HMAC-SHA256"
    )
    # Algorithm sorts before Date.
    assert canonical.index("X-Amz-Algorithm") < canonical.index("X-Amz-Date")


# --- parse round trip -------------------------------------------------------


def test_parse_reads_back_the_generated_parameters() -> None:
    query = urlsplit(_generate(expires=1800)).query
    parsed = presigned.parse_presigned_query(query)
    assert parsed is not None
    assert parsed.access_key_id == ACCESS_KEY_ID
    assert parsed.date_stamp == "20260724"
    assert parsed.region == REGION
    assert parsed.service == SERVICE
    assert parsed.amz_date == AMZ_DATE
    assert parsed.expires == 1800
    assert parsed.signed_headers == ("host",)


def test_signature_round_trips_through_recomputation() -> None:
    # What generation signed, recomputing over the same canonical request must
    # reproduce -- the guarantee the verifier depends on.
    path = presigned.object_path(BASE_PATH, BUCKET, KEY)
    query = urlsplit(_generate()).query
    parsed = presigned.parse_presigned_query(query)
    assert parsed is not None
    host = urlsplit(ENDPOINT).netloc
    request_string = presigned.presigned_canonical_request(
        "GET", path, query, {"host": host}, parsed.signed_headers
    )
    scope = sigv4.credential_scope(parsed.date_stamp, parsed.region, parsed.service)
    to_sign = sigv4.string_to_sign(parsed.amz_date, scope, request_string)
    signing_key = sigv4.signing_key(SECRET, parsed.date_stamp, parsed.region, parsed.service)
    assert sigv4.verify(sigv4.signature(signing_key, to_sign), parsed.signature)


def test_a_tampered_path_breaks_the_signature() -> None:
    query = urlsplit(_generate()).query
    parsed = presigned.parse_presigned_query(query)
    assert parsed is not None
    host = urlsplit(ENDPOINT).netloc
    other_path = presigned.object_path(BASE_PATH, BUCKET, "other.txt")
    tampered = presigned.presigned_canonical_request(
        "GET", other_path, query, {"host": host}, parsed.signed_headers
    )
    scope = sigv4.credential_scope(parsed.date_stamp, parsed.region, parsed.service)
    to_sign = sigv4.string_to_sign(parsed.amz_date, scope, tampered)
    signing_key = sigv4.signing_key(SECRET, parsed.date_stamp, parsed.region, parsed.service)
    assert not sigv4.verify(sigv4.signature(signing_key, to_sign), parsed.signature)


# --- defensive parsing ------------------------------------------------------


def test_parse_rejects_an_empty_query() -> None:
    assert presigned.parse_presigned_query("") is None


def test_parse_rejects_a_wrong_algorithm() -> None:
    query = urlsplit(_generate()).query.replace("AWS4-HMAC-SHA256", "AWS4-HMAC-SHA1")
    assert presigned.parse_presigned_query(query) is None


def test_parse_rejects_a_missing_signature() -> None:
    query = urlsplit(_generate()).query
    without = "&".join(p for p in query.split("&") if not p.startswith("X-Amz-Signature="))
    assert presigned.parse_presigned_query(without) is None


def test_parse_rejects_a_non_numeric_expiry() -> None:
    query = urlsplit(_generate()).query.replace("X-Amz-Expires=900", "X-Amz-Expires=soon")
    assert presigned.parse_presigned_query(query) is None


def test_parse_rejects_a_non_positive_expiry() -> None:
    query = urlsplit(_generate()).query.replace("X-Amz-Expires=900", "X-Amz-Expires=0")
    assert presigned.parse_presigned_query(query) is None


# --- expiry cap at seven days (AWS maximum) ---------------------------------


def test_parse_expires_rejects_over_the_cap() -> None:
    assert presigned._parse_expires(str(presigned.MAX_PRESIGN_EXPIRES + 1)) is None


def test_parse_expires_accepts_exactly_the_cap() -> None:
    assert (
        presigned._parse_expires(str(presigned.MAX_PRESIGN_EXPIRES))
        == presigned.MAX_PRESIGN_EXPIRES
    )


def test_parse_expires_accepts_a_normal_value() -> None:
    assert presigned._parse_expires("900") == 900


def test_parse_rejects_an_over_cap_expiry() -> None:
    over = presigned.MAX_PRESIGN_EXPIRES + 1
    query = urlsplit(_generate()).query.replace("X-Amz-Expires=900", f"X-Amz-Expires={over}")
    assert presigned.parse_presigned_query(query) is None


def _presign_service() -> S3PresignService:
    return S3PresignService(
        FakeKeyProvider(), endpoint=ENDPOINT, base_path=BASE_PATH, region=REGION
    )


def _access_key() -> S3AccessKey:
    keys = FakeKeyProvider()
    return S3AccessKey(
        key_id=ACCESS_KEY_ID,
        sealed_secret=keys.seal_secret(SECRET.encode()),
        secret_master_key_id=keys.master_key_id,
        label="",
        owner_id=uuid.uuid4(),
        owner_subject="user-1",
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
    )


def test_generation_refuses_an_over_cap_expires() -> None:
    with pytest.raises(InvalidArgumentError):
        _presign_service().presign(
            _access_key(),
            "GET",
            BUCKET,
            KEY,
            expires=presigned.MAX_PRESIGN_EXPIRES + 1,
        )


def test_generation_accepts_a_normal_expires() -> None:
    url = _presign_service().presign(_access_key(), "GET", BUCKET, KEY, expires=900)
    assert url.startswith(ENDPOINT)


def test_parse_rejects_a_malformed_credential_scope() -> None:
    query = urlsplit(_generate()).query
    broken = query.replace("%2Faws4_request", "%2Fwrong")
    assert presigned.parse_presigned_query(broken) is None


def test_parse_rejects_an_empty_scope_component() -> None:
    # A credential with an empty region component: right arity, blank part.
    query = urlsplit(_generate()).query.replace("%2Fus-east-1%2F", "%2F%2F")
    assert presigned.parse_presigned_query(query) is None


def test_parse_rejects_empty_signed_headers() -> None:
    query = urlsplit(_generate()).query.replace("X-Amz-SignedHeaders=host", "X-Amz-SignedHeaders=")
    assert presigned.parse_presigned_query(query) is None
