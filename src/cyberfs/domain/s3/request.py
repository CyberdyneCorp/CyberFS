"""The framework-free view of an incoming S3 request.

The inbound S3 adapter (phase 6) parses a real HTTP request into this value
object; the verifier consumes only this, so its logic is unit-testable without
FastAPI or a socket. `content_sha256` is the *signed* payload hash the client
put in `x-amz-content-sha256`; `body_sha256` is what CyberFS actually computed
over the received body (or `None` when it has not been computed, e.g. a
streamed body not yet drained). A mismatch between the two is what stops a
captured signature being replayed over swapped content.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class S3Request:
    """A parsed, signable S3 request."""

    method: str
    path: str
    query: str
    #: All request headers; lookup by the verifier is case-insensitive.
    headers: Mapping[str, str] = field(default_factory=dict)
    #: The signed `x-amz-date`, verbatim, in `YYYYMMDDThhmmssZ` form.
    amz_date: str = ""
    #: The value the client signed in `x-amz-content-sha256` (a hex digest or
    #: the literal `UNSIGNED-PAYLOAD`).
    content_sha256: str = ""
    #: The digest CyberFS computed over the received body, if computed.
    body_sha256: str | None = None
    #: The connecting IP, for per-source failure rate limiting.
    source_ip: str | None = None
