"""No S3 log line carries a secret, a signature, or object content (task 10.3).

Invariant A: nothing the S3 auth or object paths log may contain an access-key
secret, a SigV4 signature, or a file's content. This is proven two ways:

* **The real call sites are clean.** The genuine `S3SignatureVerifier` is driven
  over its header and presigned success paths with a capturing logger, and every
  line it emits -- rendered through the production redaction pipeline -- is
  asserted to contain neither the access key's secret nor the request's actual
  signature. The S3 object path logs nothing at all, so it cannot leak content.
* **Redaction is the backstop.** Were a future line to carry one of these under
  any of its plausible field names, the `redact_secrets` processor scrubs it, so
  the invariant survives a careless addition. This stands in for the object
  path's content, which no current site logs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import structlog

from cyberfs.domain.errors import SignatureMismatchError
from cyberfs.infrastructure.logging import (
    REDACTED,
    add_request_context,
    clear_request_context,
    redact_secrets,
)

from .fakes import FakeKeyProvider, FakeUnitOfWork
from .test_s3_authenticator import (
    ACCESS_KEY_ID,
    NOW,
    SECRET,
    _make_key,
    _make_verifier,
    _presigned_request,
    _seed_key,
    _signed_request,
)


@pytest.fixture(autouse=True)
def _clean_context() -> Any:
    clear_request_context()
    yield
    clear_request_context()


class RecordingLogger:
    """Stands in for the module logger, capturing every emitted line verbatim."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event: str, **kwargs: Any) -> None:
        self.lines.append((event, kwargs))

    info = warning = error = debug = exception = _record


def _rendered(event: str, kwargs: dict[str, Any]) -> str:
    """Render one line through the production security processors, as JSON."""
    payload: dict[str, Any] = {"event": event, **kwargs}
    payload = add_request_context(None, "info", payload)
    payload = redact_secrets(None, "info", payload)
    return str(structlog.processors.JSONRenderer()(None, "info", payload))


def _signature_of(authorization: str) -> str:
    """The hex signature carried in an `Authorization: AWS4-HMAC-SHA256` header."""
    _, _, signature = authorization.rpartition("Signature=")
    return signature.strip()


def _query_signature_of(query: str) -> str:
    """The hex signature carried in a presigned URL's `X-Amz-Signature`."""
    _, _, tail = query.rpartition("X-Amz-Signature=")
    return tail.split("&", 1)[0]


# --- the real S3 auth path leaks nothing ------------------------------------


@pytest.mark.asyncio
async def test_s3_auth_path_logs_no_secret_or_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeKeyProvider()
    uow = FakeUnitOfWork()
    await _seed_key(uow, _make_key(provider))
    verifier = _make_verifier(provider)

    recorder = RecordingLogger()
    monkeypatch.setattr("cyberfs.application.s3_auth.logger", recorder)

    header_request = _signed_request(signed_at=NOW)
    presigned_request = _presigned_request(signed_at=NOW)
    await verifier.verify(uow, header_request, now=NOW)
    await verifier.verify(uow, presigned_request, now=NOW)
    # A failure too, to sweep the rejection branch for stray fields.
    with pytest.raises(SignatureMismatchError):
        await verifier.verify(uow, _signed_request(signed_at=NOW, tamper_signature=True), now=NOW)

    # The verifier does log -- so this is a real assertion, not a vacuous one.
    assert recorder.lines
    header_signature = _signature_of(header_request.headers["authorization"])
    presigned_signature = _query_signature_of(presigned_request.query)
    forbidden = (SECRET, header_signature, presigned_signature)
    assert all(len(value) > 16 for value in forbidden)  # guard: real, non-empty values

    for event, kwargs in recorder.lines:
        line = _rendered(event, kwargs)
        for value in forbidden:
            assert value not in line, f"{event!r} leaked a secret or signature: {line}"
    # And the access key *id* -- a non-secret identifier -- is retained, so the
    # test proves redaction is targeted, not a blanket blackout of the line.
    assert any(ACCESS_KEY_ID in _rendered(event, kwargs) for event, kwargs in recorder.lines)


# --- redaction is the backstop for every sensitive field --------------------


@pytest.mark.parametrize(
    ("key", "secret_value"),
    [
        ("secret", "cyberfs-access-key-secret-value"),
        ("access_key_secret", "cyberfs-access-key-secret-value"),
        ("secret_access_key", "cyberfs-access-key-secret-value"),
        ("s3_secret", "cyberfs-access-key-secret-value"),
        ("signature", "5d672d79c15b13162d9279b0855cfba6789a8edb"),
        ("amz_signature", "5d672d79c15b13162d9279b0855cfba6789a8edb"),
        ("content", "the object's plaintext body"),
        ("plaintext", "the object's plaintext body"),
        ("node_name", "Q3 layoffs - final.xlsx"),
        ("filename", "Q3 layoffs - final.xlsx"),
    ],
)
def test_sensitive_s3_fields_are_scrubbed(key: str, secret_value: str) -> None:
    line = _rendered("s3_event", {key: secret_value})
    assert secret_value not in line
    assert redact_secrets(None, "info", {key: secret_value})[key] == REDACTED


def test_a_signature_in_an_authorization_header_is_scrubbed() -> None:
    authorization = (
        "AWS4-HMAC-SHA256 Credential=AKIA.../20260724/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edbdeadbeef"
    )
    assert redact_secrets(None, "info", {"authorization": authorization})["authorization"] == (
        REDACTED
    )


def test_an_object_content_body_survives_only_as_redacted() -> None:
    """The object path's content, were it ever logged, never reaches a sink."""
    body = b"binary file content that must not be logged"
    rendered = _rendered("s3_put_object", {"content": body.decode(), "plaintext": body.decode()})
    payload = json.loads(rendered)
    assert payload["content"] == REDACTED
    assert payload["plaintext"] == REDACTED
