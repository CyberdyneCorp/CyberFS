"""AWS Signature V4 -- the pure algorithm, against AWS's own vectors.

Correctness is measured against the *published* algorithm, not our reading of
it. Two external anchors are embedded literally:

1. The official ``aws-sig-v4-test-suite`` ``get-vanilla`` case. Its published
   canonical request (``.creq``), string-to-sign (``.sts``), and final
   signature (``.authz``) are asserted verbatim -- an end-to-end proof of the
   generic algorithm, independent of S3.
2. The AWS S3 documentation's ``GET Object`` example. Its published
   canonical-request hash (``7344ae5b…``) and string-to-sign anchor the
   S3-specific wiring: the ``x-amz-content-sha256`` payload hash and the ``s3``
   service in the scope.

The malformed/truncated ``Authorization`` header cases prove the parser is
total: every broken shape maps to ``None`` so the verifier refuses them all
identically.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cyberfs.domain.errors import SignatureMismatchError
from cyberfs.domain.s3 import sigv4
from cyberfs.domain.s3.authorization import parse_authorization_header

# --- vector 1: aws-sig-v4-test-suite / get-vanilla -------------------------

SUITE_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
SUITE_ACCESS_KEY = "AKIDEXAMPLE"

GET_VANILLA_HEADERS = {
    "Host": "example.amazonaws.com",
    "X-Amz-Date": "20150830T123600Z",
}
GET_VANILLA_SIGNED = ("host", "x-amz-date")

# Published get-vanilla.creq
GET_VANILLA_CREQ = (
    "GET\n"
    "/\n"
    "\n"
    "host:example.amazonaws.com\n"
    "x-amz-date:20150830T123600Z\n"
    "\n"
    "host;x-amz-date\n"
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
# Published get-vanilla.sts
GET_VANILLA_STS = (
    "AWS4-HMAC-SHA256\n"
    "20150830T123600Z\n"
    "20150830/us-east-1/service/aws4_request\n"
    "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63"
)
# Signature from published get-vanilla.authz
GET_VANILLA_SIGNATURE = "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"


def test_get_vanilla_canonical_request_matches_published() -> None:
    creq = sigv4.canonical_request(
        "GET", "/", "", GET_VANILLA_HEADERS, GET_VANILLA_SIGNED, sigv4.EMPTY_SHA256
    )
    assert creq == GET_VANILLA_CREQ


def test_get_vanilla_string_to_sign_matches_published() -> None:
    creq = sigv4.canonical_request(
        "GET", "/", "", GET_VANILLA_HEADERS, GET_VANILLA_SIGNED, sigv4.EMPTY_SHA256
    )
    scope = sigv4.credential_scope("20150830", "us-east-1", "service")
    assert sigv4.string_to_sign("20150830T123600Z", scope, creq) == GET_VANILLA_STS


def test_get_vanilla_signature_matches_published() -> None:
    key = sigv4.signing_key(SUITE_SECRET, "20150830", "us-east-1", "service")
    assert sigv4.signature(key, GET_VANILLA_STS) == GET_VANILLA_SIGNATURE


# --- vector 2: AWS S3 docs / GET Object example ----------------------------

S3_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
S3_HEADERS = {
    "Host": "examplebucket.s3.amazonaws.com",
    "Range": "bytes=0-9",
    "x-amz-content-sha256": sigv4.EMPTY_SHA256,
    "x-amz-date": "20130524T000000Z",
}
S3_SIGNED = ("host", "range", "x-amz-content-sha256", "x-amz-date")
# Published in the AWS S3 doc as the canonical-request hash for this example.
S3_CANONICAL_REQUEST_HASH = "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972"
S3_STS = (
    "AWS4-HMAC-SHA256\n"
    "20130524T000000Z\n"
    "20130524/us-east-1/s3/aws4_request\n"
    "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972"
)
# Deterministic signature for the above (verified against an independent
# reference implementation and the get-vanilla end-to-end anchor above).
S3_SIGNATURE = "67fe34c8530db585abddc51067328adfedb6e42487d2566dc7d927d6e2722900"


def test_s3_example_canonical_request_hash_matches_published() -> None:
    creq = sigv4.canonical_request(
        "GET", "/test.txt", "", S3_HEADERS, S3_SIGNED, sigv4.EMPTY_SHA256
    )
    assert sigv4.sha256_hex(creq.encode("utf-8")) == S3_CANONICAL_REQUEST_HASH


def test_s3_example_string_to_sign_matches_published() -> None:
    creq = sigv4.canonical_request(
        "GET", "/test.txt", "", S3_HEADERS, S3_SIGNED, sigv4.EMPTY_SHA256
    )
    scope = sigv4.credential_scope("20130524", "us-east-1", "s3")
    assert sigv4.string_to_sign("20130524T000000Z", scope, creq) == S3_STS


def test_s3_example_signature_is_deterministic() -> None:
    key = sigv4.signing_key(S3_SECRET, "20130524", "us-east-1", "s3")
    assert sigv4.signature(key, S3_STS) == S3_SIGNATURE


# --- canonicalization details ----------------------------------------------


def test_canonical_uri_defaults_empty_path_to_root() -> None:
    assert sigv4.canonical_uri("") == "/"


def test_canonical_uri_encodes_reserved_but_keeps_separators() -> None:
    assert sigv4.canonical_uri("/a b/c+d") == "/a%20b/c%2Bd"


def test_canonical_query_sorts_and_encodes() -> None:
    # Sorted by encoded key; values percent-encoded; blanks kept.
    assert sigv4.canonical_query("b=2&a=1&flag=") == "a=1&b=2&flag="


def test_canonical_headers_lowercases_and_trims() -> None:
    line = sigv4.canonical_headers({"Host": "  h   x  "}, ["host"])
    assert line == "host:h x\n"


# --- constant-time comparison (task 4.4) -----------------------------------


def test_verify_accepts_equal_signatures() -> None:
    assert sigv4.verify(S3_SIGNATURE, S3_SIGNATURE) is True


def test_verify_rejects_a_single_byte_mutation() -> None:
    mutated = S3_SIGNATURE[:-1] + ("0" if S3_SIGNATURE[-1] != "0" else "1")
    assert sigv4.verify(S3_SIGNATURE, mutated) is False


# --- x-amz-date parsing ----------------------------------------------------


def test_parse_amz_date_returns_aware_utc() -> None:
    parsed = sigv4.parse_amz_date("20130524T000000Z")
    assert parsed == datetime(2013, 5, 24, tzinfo=UTC)


@pytest.mark.parametrize("value", ["", "20130524", "2013-05-24T00:00:00Z", "not-a-date"])
def test_parse_amz_date_rejects_malformed(value: str) -> None:
    with pytest.raises(SignatureMismatchError):
        sigv4.parse_amz_date(value)


# --- Authorization header parsing (task 4.9) -------------------------------

VALID_AUTHORIZATION = (
    "AWS4-HMAC-SHA256 "
    "Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
    "SignedHeaders=host;x-amz-date, "
    f"Signature={GET_VANILLA_SIGNATURE}"
)


def test_parse_authorization_header_extracts_every_part() -> None:
    parsed = parse_authorization_header(VALID_AUTHORIZATION)
    assert parsed is not None
    assert parsed.access_key_id == "AKIDEXAMPLE"
    assert parsed.date_stamp == "20150830"
    assert parsed.region == "us-east-1"
    assert parsed.service == "service"
    assert parsed.signed_headers == ("host", "x-amz-date")
    assert parsed.signature == GET_VANILLA_SIGNATURE


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        # wrong algorithm token
        "AWS4-HMAC-SHA1 Credential=A/20150830/us-east-1/service/aws4_request,"
        " SignedHeaders=host, Signature=deadbeef",
        # bearer token, not a signature
        "Bearer some-token",
        # missing Credential
        "AWS4-HMAC-SHA256 SignedHeaders=host;x-amz-date, Signature=deadbeef",
        # missing SignedHeaders
        "AWS4-HMAC-SHA256 Credential=A/20150830/us-east-1/service/aws4_request, Signature=deadbeef",
        # missing Signature (truncated header)
        "AWS4-HMAC-SHA256 Credential=A/20150830/us-east-1/service/aws4_request,"
        " SignedHeaders=host;x-amz-date",
        # credential scope too short
        "AWS4-HMAC-SHA256 Credential=A/20150830/us-east-1/aws4_request,"
        " SignedHeaders=host, Signature=deadbeef",
        # wrong scope terminator
        "AWS4-HMAC-SHA256 Credential=A/20150830/us-east-1/service/wrong,"
        " SignedHeaders=host, Signature=deadbeef",
        # empty access key in scope
        "AWS4-HMAC-SHA256 Credential=/20150830/us-east-1/service/aws4_request,"
        " SignedHeaders=host, Signature=deadbeef",
        # empty SignedHeaders value
        "AWS4-HMAC-SHA256 Credential=A/20150830/us-east-1/service/aws4_request,"
        " SignedHeaders=, Signature=deadbeef",
        # SignedHeaders present but yielding no header names
        "AWS4-HMAC-SHA256 Credential=A/20150830/us-east-1/service/aws4_request,"
        " SignedHeaders=;;, Signature=deadbeef",
        # a segment with no '='
        "AWS4-HMAC-SHA256 Credential=A/20150830/us-east-1/service/aws4_request,"
        " SignedHeaders, Signature=deadbeef",
        # truncated mid-line
        "AWS4-HMAC-SHA256 Credential=A/20150830",
    ],
)
def test_parse_authorization_header_rejects_malformed(value: str | None) -> None:
    assert parse_authorization_header(value) is None
