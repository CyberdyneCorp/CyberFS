"""FastAPI authentication dependencies.

Three, matching the modes in `authentication/spec.md`:

* `CurrentPrincipal`   -- claim-based; ordinary reads and writes.
* `FreshPrincipal`     -- introspection-backed; grants, revocations, and
                          ownership transfer.
* `AdminPrincipal`     -- introspection-backed and `is_admin`; admin routes.

Keeping them distinct is what stops a revocation-sensitive route from quietly
being written against the cheap check.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cyberfs.application.authentication import AuthenticationService
from cyberfs.application.provisioning import ProvisioningService
from cyberfs.domain.auth.principal import Principal
from cyberfs.domain.errors import AuthenticationError
from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.users import User
from cyberfs.infrastructure.logging import bind_subject

#: `auto_error=False` so a missing header raises our own problem-details
#: response rather than FastAPI's, keeping the error shape uniform.
bearer_scheme = HTTPBearer(auto_error=False)

Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


def _auth_service(request: Request) -> AuthenticationService:
    service: AuthenticationService = request.app.state.authentication
    return service


def _source_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("a bearer token is required")
    return credentials.credentials


async def _authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    require_fresh: bool,
) -> Principal:
    principal = await _auth_service(request).authenticate(
        _token(credentials),
        source_ip=_source_ip(request),
        require_fresh=require_fresh,
    )
    request.state.principal = principal
    bind_subject(principal.subject)
    return principal


async def current_principal(request: Request, credentials: Credentials) -> Principal:
    return await _authenticate(request, credentials, require_fresh=False)


async def fresh_principal(request: Request, credentials: Credentials) -> Principal:
    """For operations whose outcome must reflect the identity plane *now*."""
    return await _authenticate(request, credentials, require_fresh=True)


async def admin_principal(request: Request, credentials: Credentials) -> Principal:
    principal = await _auth_service(request).authenticate_admin(
        _token(credentials),
        source_ip=_source_ip(request),
    )
    request.state.principal = principal
    bind_subject(principal.subject)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
FreshPrincipal = Annotated[Principal, Depends(fresh_principal)]
AdminPrincipal = Annotated[Principal, Depends(admin_principal)]


async def unit_of_work(request: Request) -> AsyncIterator[UnitOfWork]:
    """One transaction per request.

    Opened here and closed here; use cases decide when to commit. Anything
    left uncommitted rolls back, so a handler that forgets cannot half-save.
    """
    async with request.app.state.unit_of_work() as uow:
        yield uow


UnitOfWorkDep = Annotated[UnitOfWork, Depends(unit_of_work)]


async def current_user(request: Request, principal: CurrentPrincipal, uow: UnitOfWorkDep) -> User:
    """Resolve the verified principal to a local user, provisioning on first touch.

    Committed immediately: a caller's very first request must leave them
    provisioned even if the operation it carried goes on to fail.
    """
    provisioning: ProvisioningService = request.app.state.provisioning
    user = await provisioning.resolve(uow, principal)
    await uow.commit()
    return user


CurrentUser = Annotated[User, Depends(current_user)]
