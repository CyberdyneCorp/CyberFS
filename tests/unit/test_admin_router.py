"""The admin surface's structural guarantees.

These assert properties of the *router* rather than of any single response, so
a future route cannot quietly reintroduce content access.
"""

from __future__ import annotations

import inspect

from fastapi.routing import APIRoute

from cyberfs.adapters.inbound.api.dependencies import admin_principal
from cyberfs.adapters.inbound.api.routers import admin as admin_router
from cyberfs.application.admin import EXPECTED_JOBS, AdminService
from cyberfs.domain.cache import Dataset

ROUTES = [route for route in admin_router.router.routes if isinstance(route, APIRoute)]

#: Words that would indicate a route serving bytes rather than statistics.
CONTENT_MARKERS = ("content", "download", "raw", "preview", "thumbnail", "blob", "stream")


def test_the_admin_router_has_routes() -> None:
    assert ROUTES, "the enumeration tests would pass vacuously otherwise"


def test_no_admin_route_serves_content() -> None:
    """`admin-dashboard/spec.md`: the surface contains no content route."""
    offenders = [
        route.path
        for route in ROUTES
        if any(marker in route.path.lower() for marker in CONTENT_MARKERS)
    ]
    assert not offenders, f"content-shaped admin routes: {offenders}"


def test_no_admin_route_returns_a_streaming_response() -> None:
    """A stream is how bytes would leave; none may."""
    from fastapi.responses import FileResponse, StreamingResponse

    offenders = []
    for route in ROUTES:
        annotation = inspect.signature(route.endpoint).return_annotation
        if annotation in (StreamingResponse, FileResponse):
            offenders.append(route.path)
    assert not offenders, f"streaming admin routes: {offenders}"


def test_every_admin_route_requires_an_admin_principal() -> None:
    """Introspection-backed, so a demoted admin is denied at once."""
    unguarded = []
    for route in ROUTES:
        dependencies = {dependency.call for dependency in route.dependant.dependencies}
        if admin_principal not in dependencies and not _has_nested(route, admin_principal):
            unguarded.append(route.path)
    assert not unguarded, f"admin routes without an admin dependency: {unguarded}"


def _has_nested(route: APIRoute, target: object) -> bool:
    stack = list(route.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        if dependency.call is target:
            return True
        stack.extend(dependency.dependencies)
    return False


def test_every_admin_route_is_under_the_admin_prefix() -> None:
    assert all(route.path.startswith("/api/v1/admin") for route in ROUTES)


def test_the_response_models_expose_no_key_material() -> None:
    """No admin response model may carry a field that could hold a key."""
    forbidden = {"wrapped_dek", "wrapped_kek", "dek", "kek", "master_key", "token_hash"}
    offenders = []
    for route in ROUTES:
        model = route.response_model
        fields = getattr(model, "model_fields", {})
        offenders.extend(name for name in fields if name in forbidden)
    assert not offenders, f"key-material fields on admin responses: {offenders}"


# --- refusals --------------------------------------------------------------


def test_grant_revocation_is_refused_not_silently_missing() -> None:
    """The route exists so the refusal is discoverable, not a 404."""
    import pytest

    from cyberfs.domain.errors import PermissionDeniedError

    paths = [route.path for route in ROUTES]
    assert "/api/v1/admin/nodes/{node_id}/grants/{subject}" in paths

    with pytest.raises(PermissionDeniedError, match="node owner"):
        AdminService().refuse_grant_revocation()


def test_there_is_no_route_for_an_admin_to_grant_themselves_access() -> None:
    creating = [
        route.path
        for route in ROUTES
        if "grant" in route.path and {"POST", "PUT"} & set(route.methods or ())
    ]
    assert not creating, f"admin grant-creation routes: {creating}"


# --- service surface -------------------------------------------------------


def test_the_expected_jobs_are_the_ones_the_spec_names() -> None:
    assert set(EXPECTED_JOBS) == {
        "purge",
        "orphan_reaper",
        "reconcile_quotas",
        "backup",
        "activity_prune",
        "rewrap",
    }


def test_purgeable_datasets_match_the_cache() -> None:
    assert set(AdminService.cacheable_datasets()) == {str(d) for d in Dataset}
