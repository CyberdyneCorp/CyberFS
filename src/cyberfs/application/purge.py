"""The destructive half of deletion, in one place.

Soft delete only moves bytes between quota buckets; this is what actually frees
space. Both the retention sweep (`PurgeJob`) and the on-demand endpoint call
`purge_one`, so the timer and the request cannot drift apart -- and drift here
means either a quota leak or a stored object that no metadata references.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from cyberfs.domain.ports.repositories import UnitOfWork
from cyberfs.domain.ports.storage import ObjectStore


@dataclass(frozen=True, slots=True)
class Purged:
    """What one purge destroyed."""

    bytes_reclaimed: int = 0
    objects_deleted: int = 0
    nodes_deleted: int = 0

    def __add__(self, other: Purged) -> Purged:
        return Purged(
            self.bytes_reclaimed + other.bytes_reclaimed,
            self.objects_deleted + other.objects_deleted,
            self.nodes_deleted + other.nodes_deleted,
        )


async def purge_one(
    uow: UnitOfWork,
    objects: ObjectStore,
    node_id: uuid.UUID,
    now: datetime,
) -> Purged:
    """Destroy one node: its objects, versions, wrapped keys, grants, and row.

    Quota is released against the node's *owner*, never the caller -- an
    administrator purging someone else's trash must free that user's space.

    Public links need no explicit delete: `PublicLinkRow.node_id` cascades, so
    removing the row takes them with it.

    Does not commit. The caller owns the transaction boundary, which is what
    makes a partial failure retryable: metadata survives, and deleting an object
    that is already gone is a no-op.
    """
    node = await uow.nodes.get(node_id)
    if node is None:
        return Purged()

    usage = await uow.quotas.get(node.owner_id)
    if usage is not None:
        usage.purge_from_trash(node.size_bytes, now)
        await uow.quotas.update(usage)

    versions = await uow.versions.list_for_node(node_id)
    for version in versions:
        await objects.delete(version.object_key)
        await uow.versions.delete(version.id)

    # Grants and wrapped keys go with it: a purged node must leave no way for a
    # former recipient to reach anything.
    await uow.grants.delete_for_node(node_id)
    await uow.keys.delete_data_keys_for_node(node_id)
    await uow.nodes.delete_permanently(node_id)

    return Purged(node.size_bytes, len(versions), 1)
