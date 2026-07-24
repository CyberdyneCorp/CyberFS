"""Small async byte-stream helpers shared by the backup and restore use cases.

The object store speaks `AsyncIterator[bytes]`; these adapt a single buffer to
that shape and back. Pure standard library, so the application layer keeps its
distance from any adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator


async def once(data: bytes) -> AsyncIterator[bytes]:
    """Yield a single buffer as a one-shot async stream.

    Lets a manifest -- small and already in memory -- be handed to the store's
    streaming `put` without a bespoke iterator at each call site.
    """
    yield data


async def collect(source: AsyncIterator[bytes]) -> bytes:
    """Drain a stream into one buffer.

    Only used for artifacts known to be small (the manifest); content objects
    are never collected, they stream straight through.
    """
    buffer = bytearray()
    async for chunk in source:
        buffer.extend(chunk)
    return bytes(buffer)
