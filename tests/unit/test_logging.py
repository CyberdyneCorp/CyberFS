"""Logging redaction and request correlation."""

from __future__ import annotations

import pytest

from cyberfs.infrastructure.logging import (
    REDACTED,
    add_request_context,
    bind_request_context,
    bind_subject,
    clear_request_context,
    current_request_id,
    redact_secrets,
)


@pytest.fixture(autouse=True)
def _clean_context() -> object:
    clear_request_context()
    yield
    clear_request_context()


def redact(**event: object) -> dict[str, object]:
    return redact_secrets(None, "info", dict(event))


def stamp(**event: object) -> dict[str, object]:
    return add_request_context(None, "info", dict(event))


# --- redaction -------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "authorization",
        "access_token",
        "refresh_token",
        "password",
        "passphrase",
        "client_secret",
        "master_key",
        "dek",
        "kek",
        "wrapped_key",
        "nonce",
        "tag",
        "ciphertext",
        "plaintext",
        "content",
        "public_link_token",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    assert redact(**{key: "super-secret-value"})[key] == REDACTED


def test_filenames_are_redacted_by_default() -> None:
    """Names are content-adjacent -- `admin-dashboard/spec.md`."""
    assert redact(filename="Q3 layoffs - final.xlsx")["filename"] == REDACTED
    assert redact(node_name="passwords.kdbx")["node_name"] == REDACTED


def test_redaction_is_case_insensitive() -> None:
    assert redact(Authorization="Bearer abc")["Authorization"] == REDACTED


def test_nested_secrets_are_redacted() -> None:
    result = redact(context={"password": "hunter2", "node_id": "n-1"})
    assert result["context"] == {"password": REDACTED, "node_id": "n-1"}


def test_bearer_token_in_free_text_is_scrubbed() -> None:
    result = redact(event="rejected request with Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")
    assert "eyJhbGciOiJIUzI1NiJ9" not in str(result["event"])
    assert REDACTED in str(result["event"])


def test_ordinary_fields_survive() -> None:
    result = redact(event="node created", node_id="n-1", size_bytes=42)
    assert result == {"event": "node created", "node_id": "n-1", "size_bytes": 42}


def test_reason_codes_are_not_redacted() -> None:
    """Audit records need the reason to be readable -- see `authentication/spec.md`."""
    assert redact(reason="token_expired", status=401)["reason"] == "token_expired"


# --- request correlation ---------------------------------------------------


def test_request_id_is_stamped() -> None:
    bind_request_context("req-123")
    assert stamp(event="hello")["request_id"] == "req-123"


def test_subject_is_stamped_once_authenticated() -> None:
    bind_request_context("req-123")
    bind_subject("user-abc")
    stamped = stamp(event="hello")
    assert stamped["request_id"] == "req-123"
    assert stamped["subject"] == "user-abc"


def test_unauthenticated_lines_carry_no_subject() -> None:
    bind_request_context("req-123")
    assert "subject" not in stamp(event="hello")


def test_context_absent_before_a_request() -> None:
    assert current_request_id() is None
    assert stamp(event="startup") == {"event": "startup"}


def test_context_cleared_between_requests() -> None:
    bind_request_context("req-1", "user-a")
    clear_request_context()
    assert stamp(event="next") == {"event": "next"}
