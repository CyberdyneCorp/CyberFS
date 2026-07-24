"""Two-mode authentication -- `authentication/spec.md`.

Ordinary reads authorize from claims; revocation-sensitive operations
introspect and the introspection result wins.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cyberfs.application.authentication import AUTH_FAILURE_WINDOW, AuthenticationService
from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import (
    DependencyUnavailableError,
    InvalidTokenError,
    PermissionDeniedError,
    RateLimitedError,
    TokenExpiredError,
)
from cyberfs.domain.ratelimit import FixedWindowLimiter

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
IP = "203.0.113.9"


class StubIdentity:
    """Stands in for both the verifier and the introspector."""

    def __init__(
        self,
        principal: Principal | None = None,
        error: Exception | None = None,
    ) -> None:
        self.principal = principal
        self.error = error
        self.calls = 0

    async def _result(self) -> Principal:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.principal is not None
        return self.principal

    async def verify(self, token: str) -> Principal:
        return await self._result()

    async def introspect(self, token: str) -> Principal:
        return await self._result()


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[AuditRecord] = []

    async def record(self, entry: AuditRecord) -> None:
        self.entries.append(entry)


def user(subject: str = "user-1", *, is_admin: bool = False) -> Principal:
    return Principal(subject=subject, is_admin=is_admin, expires_at=NOW + timedelta(minutes=5))


def build(
    verifier: StubIdentity,
    introspector: StubIdentity | None = None,
    *,
    limit: int = 3,
) -> tuple[AuthenticationService, RecordingAudit]:
    audit = RecordingAudit()
    service = AuthenticationService(
        verifier=verifier,
        introspector=introspector or StubIdentity(user()),
        audit=audit,
        failure_limiter=FixedWindowLimiter(limit=limit, window=AUTH_FAILURE_WINDOW),
    )
    return service, audit


# --- claim-based mode ------------------------------------------------------


async def test_ordinary_read_uses_the_local_claim() -> None:
    verifier = StubIdentity(user())
    introspector = StubIdentity(user())
    service, _ = build(verifier, introspector)

    principal = await service.authenticate("tok", now=NOW)

    assert principal.subject == "user-1"
    assert verifier.calls == 1
    assert introspector.calls == 0, "an ordinary read must not call introspection"


async def test_invalid_token_is_rejected() -> None:
    service, _ = build(StubIdentity(error=InvalidTokenError()))
    with pytest.raises(InvalidTokenError):
        await service.authenticate("tok", now=NOW)


async def test_expired_token_is_rejected() -> None:
    service, _ = build(StubIdentity(error=TokenExpiredError()))
    with pytest.raises(TokenExpiredError):
        await service.authenticate("tok", now=NOW)


# --- introspection-backed mode ---------------------------------------------


async def test_fresh_mode_verifies_locally_and_then_introspects() -> None:
    verifier = StubIdentity(user())
    introspector = StubIdentity(user())
    service, _ = build(verifier, introspector)

    await service.authenticate("tok", require_fresh=True, now=NOW)

    assert verifier.calls == 1, "a garbage token should be rejected before a round trip"
    assert introspector.calls == 1


async def test_introspection_result_overrides_the_claim() -> None:
    """A demoted admin still holds a token whose is_admin claim says otherwise."""
    verifier = StubIdentity(user(is_admin=True))
    introspector = StubIdentity(user(is_admin=False))
    service, _ = build(verifier, introspector)

    principal = await service.authenticate("tok", require_fresh=True, now=NOW)

    assert not principal.is_admin


async def test_revoked_token_is_blocked_from_a_fresh_operation() -> None:
    verifier = StubIdentity(user())
    introspector = StubIdentity(error=InvalidTokenError("token is not active"))
    service, _ = build(verifier, introspector)

    with pytest.raises(InvalidTokenError):
        await service.authenticate("tok", require_fresh=True, now=NOW)


async def test_introspection_outage_fails_closed() -> None:
    """Never fall back to the local claim -- that would defeat asking."""
    verifier = StubIdentity(user(is_admin=True))
    introspector = StubIdentity(error=DependencyUnavailableError())
    service, _ = build(verifier, introspector)

    with pytest.raises(DependencyUnavailableError):
        await service.authenticate("tok", require_fresh=True, now=NOW)


async def test_introspection_outage_is_not_counted_against_the_caller() -> None:
    """Our outage is not the caller's fault; it must not consume their allowance."""
    verifier = StubIdentity(user())
    introspector = StubIdentity(error=DependencyUnavailableError())
    service, audit = build(verifier, introspector, limit=1)

    for _ in range(3):
        with pytest.raises(DependencyUnavailableError):
            await service.authenticate("tok", require_fresh=True, now=NOW, source_ip=IP)

    assert not any(e.action is AuditAction.AUTH_DENIED for e in audit.entries)


# --- admin -----------------------------------------------------------------


async def test_admin_is_admitted() -> None:
    service, _ = build(StubIdentity(user(is_admin=True)), StubIdentity(user(is_admin=True)))
    principal = await service.authenticate_admin("tok", now=NOW)
    assert principal.is_admin


async def test_non_admin_is_denied() -> None:
    service, _ = build(StubIdentity(user()), StubIdentity(user()))
    with pytest.raises(PermissionDeniedError):
        await service.authenticate_admin("tok", now=NOW)


async def test_demoted_admin_is_denied_without_waiting_for_expiry() -> None:
    verifier = StubIdentity(user(is_admin=True))
    introspector = StubIdentity(user(is_admin=False))
    service, _ = build(verifier, introspector)

    with pytest.raises(PermissionDeniedError):
        await service.authenticate_admin("tok", now=NOW)


async def test_admin_route_always_introspects() -> None:
    verifier = StubIdentity(user(is_admin=True))
    introspector = StubIdentity(user(is_admin=True))
    service, _ = build(verifier, introspector)

    await service.authenticate_admin("tok", now=NOW)

    assert introspector.calls == 1


async def test_admin_denial_is_audited_with_the_subject() -> None:
    service, audit = build(StubIdentity(user()), StubIdentity(user()))
    with pytest.raises(PermissionDeniedError):
        await service.authenticate_admin("tok", now=NOW, source_ip=IP)

    denial = next(e for e in audit.entries if e.action is AuditAction.AUTH_DENIED)
    assert denial.actor_subject == "user-1"
    assert denial.reason == "permission_denied"


# --- auditing --------------------------------------------------------------


async def test_denial_is_audited_with_reason_and_ip() -> None:
    service, audit = build(StubIdentity(error=TokenExpiredError()))
    with pytest.raises(TokenExpiredError):
        await service.authenticate("tok", now=NOW, source_ip=IP)

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.action is AuditAction.AUTH_DENIED
    assert entry.reason == "token_expired"
    assert entry.source_ip == IP


async def test_audit_record_never_holds_the_token() -> None:
    service, audit = build(StubIdentity(error=InvalidTokenError()))
    with pytest.raises(InvalidTokenError):
        await service.authenticate("super-secret-token", now=NOW, source_ip=IP)

    assert "super-secret-token" not in repr(audit.entries)


async def test_success_is_not_audited_as_a_denial() -> None:
    service, audit = build(StubIdentity(user()))
    await service.authenticate("tok", now=NOW, source_ip=IP)
    assert audit.entries == []


# --- rate limiting ---------------------------------------------------------


async def test_repeated_failures_trip_the_limiter() -> None:
    service, _ = build(StubIdentity(error=InvalidTokenError()), limit=3)

    for _ in range(3):
        with pytest.raises(InvalidTokenError):
            await service.authenticate("tok", now=NOW, source_ip=IP)

    with pytest.raises(RateLimitedError):
        await service.authenticate("tok", now=NOW, source_ip=IP)


async def test_rate_limit_carries_retry_after() -> None:
    service, _ = build(StubIdentity(error=InvalidTokenError()), limit=1)
    with pytest.raises(InvalidTokenError):
        await service.authenticate("tok", now=NOW, source_ip=IP)

    with pytest.raises(RateLimitedError) as exc:
        await service.authenticate("tok", now=NOW, source_ip=IP)

    assert exc.value.context["retry_after_seconds"] > 0


async def test_rate_limit_is_per_ip() -> None:
    service, _ = build(StubIdentity(error=InvalidTokenError()), limit=1)
    with pytest.raises(InvalidTokenError):
        await service.authenticate("tok", now=NOW, source_ip=IP)

    with pytest.raises(InvalidTokenError):
        await service.authenticate("tok", now=NOW, source_ip="198.51.100.4")


async def test_rate_limit_lifts_after_the_window() -> None:
    service, _ = build(StubIdentity(error=InvalidTokenError()), limit=1)
    with pytest.raises(InvalidTokenError):
        await service.authenticate("tok", now=NOW, source_ip=IP)
    with pytest.raises(RateLimitedError):
        await service.authenticate("tok", now=NOW, source_ip=IP)

    with pytest.raises(InvalidTokenError):
        await service.authenticate("tok", now=NOW + AUTH_FAILURE_WINDOW, source_ip=IP)


async def test_rate_limiting_is_audited() -> None:
    service, audit = build(StubIdentity(error=InvalidTokenError()), limit=1)
    with pytest.raises(InvalidTokenError):
        await service.authenticate("tok", now=NOW, source_ip=IP)
    with pytest.raises(RateLimitedError):
        await service.authenticate("tok", now=NOW, source_ip=IP)

    assert any(e.action is AuditAction.AUTH_RATE_LIMITED for e in audit.entries)


async def test_successful_calls_are_never_rate_limited() -> None:
    service, _ = build(StubIdentity(user()), limit=1)
    for _ in range(5):
        await service.authenticate("tok", now=NOW, source_ip=IP)


async def test_requests_without_a_client_ip_are_not_limited() -> None:
    service, _ = build(StubIdentity(error=InvalidTokenError()), limit=1)
    for _ in range(3):
        with pytest.raises(InvalidTokenError):
            await service.authenticate("tok", now=NOW, source_ip=None)
