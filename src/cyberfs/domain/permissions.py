"""Effective permission.

The maximum over ownership, direct grants, and ancestor grants -- no deny
rule, so "why can Bob read this?" is answered by walking to the root and taking
the highest role found. See `design.md`, "Ordered roles with tree inheritance".
"""

from __future__ import annotations

from collections.abc import Iterable

from cyberfs.domain.errors import NotFoundError, PermissionDeniedError
from cyberfs.domain.sharing import Role


def resolve_effective_role(*, is_owner: bool, granted: Iterable[Role] = ()) -> Role | None:
    """The caller's role on a node, or `None` if they hold no access at all.

    Monotonic by construction: adding a grant can only raise the result, never
    lower it. That is what keeps the model explainable and its cache entries
    safely invalidatable by subtree.
    """
    candidates = list(granted)
    if is_owner:
        candidates.append(Role.OWNER)
    return max(candidates, default=None)


def require_role(effective: Role | None, minimum: Role, *, node_id: object = None) -> Role:
    """Assert the caller holds at least `minimum`.

    The distinction matters: a caller with *no* access gets `404`, so the API
    does not disclose that a node exists -- `file-storage/spec.md`, "Download
    denied without permission". A caller who can see the node but may not
    perform this operation gets `403`, which is not a disclosure because they
    already know it is there.
    """
    if effective is None:
        raise NotFoundError("node not found", node_id=str(node_id) if node_id else None)
    if effective < minimum:
        raise PermissionDeniedError(
            f"{minimum.slug} permission is required",
            node_id=str(node_id) if node_id else None,
            required=minimum.slug,
            held=effective.slug,
        )
    return effective
