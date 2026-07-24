"""Authenticating a caller.

Two modes, per `authentication/spec.md`:

* ordinary reads authorize from verified JWT claims -- no round trip;
* revocation-sensitive operations (admin actions, grants, revocations,
  ownership transfer) additionally introspect, and the introspection result
  wins, so a just-demoted admin is denied without waiting for their token to
  expire.

Introspection failing is *our* outage, so it fails closed with a dependency
error and is deliberately not counted against the caller's rate limit.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from cyberfs.domain.audit import AuditAction, AuditRecord
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import (
    AuthenticationError,
    PermissionDeniedError,
    RateLimitedError,
)
from cyberfs.domain.ports.audit import AuditSink
from cyberfs.domain.ports.identity import TokenIntrospector, TokenVerifier
from cyberfs.domain.ratelimit import FixedWindowLimiter

AUTH_FAILURE_WINDOW = timedelta(minutes=1)


class AuthenticationService:
    def __init__(
        self,
        verifier: TokenVerifier,
        introspector: TokenIntrospector,
        audit: AuditSink,
        failure_limiter: FixedWindowLimiter,
    ) -> None:
        self._verifier = verifier
        self._introspector = introspector
        self._audit = audit
        self._limiter = failure_limiter

    async def authenticate(
        self,
        token: str,
        *,
        source_ip: str | None = None,
        require_fresh: bool = False,
        now: datetime | None = None,
    ) -> Principal:
        moment = now or utcnow()
        await self._ensure_not_rate_limited(source_ip, moment)
        try:
            return await self._resolve(token, require_fresh)
        except AuthenticationError as exc:
            await self._record_denial(exc.code, source_ip, moment)
            raise

    async def authenticate_admin(
        self,
        token: str,
        *,
        source_ip: str | None = None,
        now: datetime | None = None,
    ) -> Principal:
        moment = now or utcnow()
        principal = await self.authenticate(
            token, source_ip=source_ip, require_fresh=True, now=moment
        )
        if not principal.is_admin:
            await self._record_denial(
                PermissionDeniedError.code, source_ip, moment, subject=principal.subject
            )
            raise PermissionDeniedError("administrator privilege is required")
        return principal

    async def _resolve(self, token: str, require_fresh: bool) -> Principal:
        """Always verify locally; introspect as well when freshness matters."""
        principal = await self._verifier.verify(token)
        if not require_fresh:
            return principal
        # Authoritative: a demoted admin still holds a structurally valid token
        # whose `is_admin` claim is stale.
        return await self._introspector.introspect(token)

    async def _ensure_not_rate_limited(self, source_ip: str | None, now: datetime) -> None:
        if source_ip is None or not self._limiter.is_limited(source_ip, now):
            return
        await self._audit.record(
            AuditRecord(
                action=AuditAction.AUTH_RATE_LIMITED,
                occurred_at=now,
                source_ip=source_ip,
                reason="too_many_auth_failures",
            )
        )
        raise RateLimitedError(
            "too many authentication failures",
            retry_after_seconds=int(self._limiter.retry_after(source_ip, now).total_seconds()),
        )

    async def _record_denial(
        self,
        reason: str,
        source_ip: str | None,
        now: datetime,
        subject: str | None = None,
    ) -> None:
        if source_ip is not None:
            self._limiter.record(source_ip, now)
        await self._audit.record(
            AuditRecord(
                action=AuditAction.AUTH_DENIED,
                occurred_at=now,
                actor_subject=subject,
                source_ip=source_ip,
                reason=reason,
            )
        )
