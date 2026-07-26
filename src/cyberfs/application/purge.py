"""The destructive half of deletion, in one place.

Soft delete only moves bytes between quota buckets; this is what actually frees
space. Both the retention sweep (`PurgeJob`) and the on-demand endpoint call
`purge_one`, so the timer and the request cannot drift apart -- and drift here
means either a quota leak or a stored object that no metadata references.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
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


async def _strip(
    uow: UnitOfWork,
    objects: ObjectStore,
    node_id: uuid.UUID,
    now: datetime,
) -> Purged:
    """Destroy everything hanging off one node, but leave its row.

    Splitting the row deletion out is what makes a subtree purge safe: rows must
    not go until every node's objects are out of the store, because
    `NodeRow.parent_id` cascades and would take a descendant's row -- and with it
    any chance of finding its object key -- before we had used it.
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

    # Grants and wrapped keys go too: a purged node must leave no way for a
    # former recipient to reach anything.
    await uow.grants.delete_for_node(node_id)
    await uow.keys.delete_data_keys_for_node(node_id)

    return Purged(node.size_bytes, len(versions), 1)


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
    stripped = await _strip(uow, objects, node_id, now)
    if stripped.nodes_deleted:
        await uow.nodes.delete_permanently(node_id)
    return stripped


async def purge_subtree(
    uow: UnitOfWork,
    objects: ObjectStore,
    node_ids: Sequence[uuid.UUID],
    root_id: uuid.UUID,
    now: datetime,
) -> Purged:
    """Destroy a whole subtree, in an order the FK cascade cannot disrupt.

    Two passes on purpose. Deleting rows as we walk would let the FK cascade on
    `NodeRow.parent_id` remove a descendant's row before its turn -- silently
    skipping its object, which then sits in the store with nothing referencing
    it. So every node is stripped of its objects first, and rows go only after.

    Order-independent by construction, rather than depending on the repository
    returning descendants deepest-first. Rows are deleted explicitly rather than
    left to the cascade, so the behaviour does not vary with how much of the
    tree the database happens to remove for us.
    """
    total = Purged()
    for node_id in node_ids:
        total += await _strip(uow, objects, node_id, now)
    for node_id in node_ids:
        await uow.nodes.delete_permanently(node_id)
    # Belt and braces: the root last, in case a descendant list was incomplete.
    await uow.nodes.delete_permanently(root_id)
    return total
