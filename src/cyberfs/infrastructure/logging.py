"""Structured JSON logging with request correlation and secret redaction.

`deployment/spec.md` requires that log lines are JSON, carry a request id
propagated from `X-Request-ID`, include the caller subject when authenticated,
and never contain file content, file names by default, bearer tokens, or key
material. Redaction happens in a processor so it applies to every line
regardless of which module emitted it.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict, Processor

REDACTED = "[redacted]"

# Keys whose values are secret wherever they appear in an event dict.
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "bearer",
        "password",
        "passphrase",
        "secret",
        "client_secret",
        "master_key",
        "master_key_previous",
        "kek",
        "dek",
        "data_key",
        "wrapped_key",
        "key_material",
        "nonce",
        "tag",
        "ciphertext",
        "plaintext",
        "content",
        "minio_secret_key",
        "cyberfs_client_secret",
        "public_link_token",
        "link_secret",
        # File names are content-adjacent: "Q3 layoffs - final.xlsx" leaks the
        # thing the user wanted private. Logged only when explicitly enabled.
        "filename",
        "file_name",
        "node_name",
    }
)

# Bearer tokens pasted into a free-text message.
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_subject: ContextVar[str | None] = ContextVar("subject", default=None)


def bind_request_context(request_id: str | None, subject: str | None = None) -> None:
    _request_id.set(request_id)
    _subject.set(subject)


def bind_subject(subject: str | None) -> None:
    """Attach the caller once authentication has resolved a principal."""
    _subject.set(subject)


def clear_request_context() -> None:
    _request_id.set(None)
    _subject.set(None)


def current_request_id() -> str | None:
    return _request_id.get()


def add_request_context(_logger: Any, _name: str, event: EventDict) -> EventDict:
    """Stamp every line with the request id and caller subject, when known."""
    if (request_id := _request_id.get()) is not None:
        event["request_id"] = request_id
    if (subject := _subject.get()) is not None:
        event["subject"] = subject
    return event


def _redact_value(key: str, value: Any) -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, str):
        return _BEARER_RE.sub(f"Bearer {REDACTED}", value)
    return value


def redact_secrets(_logger: Any, _name: str, event: EventDict) -> EventDict:
    """Drop secret values before they reach a sink."""
    return {key: _redact_value(key, value) for key, value in event.items()}


def _renderer(json_output: bool) -> Processor:
    if json_output:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure structlog and route stdlib logging through it."""
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric_level, force=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_request_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_secrets,
            _renderer(json_output),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
