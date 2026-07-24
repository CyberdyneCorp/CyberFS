"""Framed AES-256-GCM content sealing.

Each frame gets a fresh 96-bit nonce. A data key is unique to one file
version, and a version holds at most a few hundred thousand frames, so random
nonces sit far below the birthday bound -- and storing the nonce inline keeps
the format auditable rather than depending on a derivation nobody can check.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cyberfs.domain.errors import IntegrityFailureError
from cyberfs.domain.framing import (
    FRAME_OVERHEAD,
    HEADER_BYTES,
    NONCE_BYTES,
    FrameSpan,
    associated_data,
    ciphertext_size,
    decode_header,
    encode_header,
    sealed_frame_bytes,
)
from cyberfs.infrastructure.logging import get_logger

logger = get_logger(__name__)

KEY_BYTES = 32


class AesGcmContentCipher:
    """Implements the `ContentCipher` port."""

    def __init__(self, frame_bytes: int) -> None:
        self._frame_bytes = frame_bytes

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    def generate_key(self) -> bytes:
        """A fresh 256-bit data key, unique to one file version."""
        return os.urandom(KEY_BYTES)

    async def seal(
        self, plaintext: AsyncIterator[bytes], key: bytes, version_id: bytes
    ) -> AsyncIterator[bytes]:
        """Frame and seal a plaintext stream.

        Emits the header, then one sealed frame per `frame_bytes` of input.
        Memory stays at one frame regardless of file size.
        """
        cipher = AESGCM(key)
        yield encode_header(self._frame_bytes)

        buffer = bytearray()
        index = 0
        async for chunk in plaintext:
            buffer.extend(chunk)
            while len(buffer) > self._frame_bytes:
                # Strictly greater: a frame is only emitted once we know more
                # follows, so the final frame can be marked as such.
                frame = bytes(buffer[: self._frame_bytes])
                del buffer[: self._frame_bytes]
                yield self._seal_frame(cipher, frame, version_id, index, final=False)
                index += 1

        # Whatever remains is the last frame -- possibly empty, which is how
        # zero-length content still gets a final frame to mark.
        yield self._seal_frame(cipher, bytes(buffer), version_id, index, final=True)

    def _seal_frame(
        self, cipher: AESGCM, frame: bytes, version_id: bytes, index: int, *, final: bool
    ) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        aad = associated_data(version_id, index, final=final)
        return nonce + cipher.encrypt(nonce, frame, aad)

    async def open(
        self,
        ciphertext: AsyncIterator[bytes],
        key: bytes,
        version_id: bytes,
        *,
        span: FrameSpan | None = None,
    ) -> AsyncIterator[bytes]:
        """Decrypt a sealed stream, verifying every frame.

        `span` decrypts a sub-range: the caller has already fetched only those
        frames, so indices start at `span.first_index` and the output is
        trimmed to exactly the bytes requested.
        """
        cipher = AESGCM(key)
        reader = _FrameReader(ciphertext, self._frame_bytes, skip_header=span is None)
        frame_bytes = await reader.frame_size()

        index = span.first_index if span else 0
        remaining = span.take if span else None
        lead_skip = span.lead_skip if span else 0
        saw_final = False

        async for sealed in reader.frames(frame_bytes):
            plain = self._open_frame(cipher, sealed, version_id, index)
            saw_final = plain.final
            piece = plain.data[lead_skip:]
            lead_skip = 0
            if remaining is not None:
                piece = piece[:remaining]
                remaining -= len(piece)
            if piece:
                yield piece
            index += 1
            if remaining == 0:
                # A range read stops early and therefore never sees the final
                # frame; that is expected, not truncation.
                return

        if span is None and not saw_final:
            logger.error("ciphertext_truncated", version=version_id.hex())
            raise IntegrityFailureError("stored content is truncated")

    def _open_frame(
        self, cipher: AESGCM, sealed: bytes, version_id: bytes, index: int
    ) -> _OpenedFrame:
        """Try the frame as non-final, then as final.

        The flag is authenticated, so it cannot be read before verifying --
        only guessed. Two attempts is the cost of making truncation detectable.
        """
        nonce, body = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
        for final in (False, True):
            try:
                aad = associated_data(version_id, index, final=final)
                return _OpenedFrame(cipher.decrypt(nonce, body, aad), final=final)
            except InvalidTag:
                continue
        logger.error("frame_authentication_failed", index=index, version=version_id.hex())
        # Deliberately opaque: no nonce, tag, or ciphertext reaches the caller.
        raise IntegrityFailureError("stored content failed authentication")

    def ciphertext_length(self, plaintext_bytes: int) -> int:
        """Stored size for a given plaintext size, header and tags included."""
        return ciphertext_size(plaintext_bytes, self._frame_bytes)


class _OpenedFrame:
    __slots__ = ("data", "final")

    def __init__(self, data: bytes, *, final: bool) -> None:
        self.data = data
        self.final = final


class _FrameReader:
    """Reassembles whole sealed frames from an arbitrary byte stream."""

    def __init__(
        self, source: AsyncIterator[bytes], default_frame_bytes: int, *, skip_header: bool
    ) -> None:
        self._source = source
        self._buffer = bytearray()
        self._default = default_frame_bytes
        self._skip_header = skip_header
        self._frame_bytes: int | None = None

    async def _pull(self, wanted: int) -> bool:
        while len(self._buffer) < wanted:
            try:
                chunk = await anext(self._source)
            except StopAsyncIteration:
                return False
            self._buffer.extend(chunk)
        return True

    async def frame_size(self) -> int:
        """Read the header when present; a range read starts mid-object."""
        if not self._skip_header:
            self._frame_bytes = self._default
            return self._default
        if not await self._pull(HEADER_BYTES):
            raise IntegrityFailureError("stored content is empty or truncated")
        header = bytes(self._buffer[:HEADER_BYTES])
        del self._buffer[:HEADER_BYTES]
        self._frame_bytes = decode_header(header)
        return self._frame_bytes

    async def frames(self, frame_bytes: int) -> AsyncIterator[bytes]:
        sealed_size = sealed_frame_bytes(frame_bytes)
        while True:
            if not await self._pull(sealed_size):
                # A short tail is the last frame; anything smaller than the
                # per-frame overhead cannot be a frame at all.
                if len(self._buffer) > FRAME_OVERHEAD - 1:
                    yield bytes(self._buffer)
                    self._buffer.clear()
                elif self._buffer:
                    raise IntegrityFailureError("stored content ends mid-frame")
                return
            yield bytes(self._buffer[:sealed_size])
            del self._buffer[:sealed_size]
