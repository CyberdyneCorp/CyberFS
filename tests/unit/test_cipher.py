"""Framed AES-256-GCM sealing -- `content-encryption/spec.md`."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from cyberfs.adapters.outbound.cipher import AesGcmContentCipher
from cyberfs.domain.errors import IntegrityFailureError
from cyberfs.domain.framing import (
    HEADER_BYTES,
    NONCE_BYTES,
    frames_for_range,
    sealed_frame_bytes,
)

FRAME = 1024
VERSION = uuid.uuid4().bytes
OTHER_VERSION = uuid.uuid4().bytes
KEY = b"\x11" * 32
OTHER_KEY = b"\x22" * 32


def cipher(frame: int = FRAME) -> AesGcmContentCipher:
    return AesGcmContentCipher(frame)


async def stream(payload: bytes, chunk: int = 100) -> AsyncIterator[bytes]:
    for start in range(0, len(payload), chunk):
        yield payload[start : start + chunk]
    if not payload:
        return


async def collect(source: AsyncIterator[bytes]) -> bytes:
    buffer = bytearray()
    async for piece in source:
        buffer.extend(piece)
    return bytes(buffer)


async def seal(payload: bytes, *, frame: int = FRAME, key: bytes = KEY) -> bytes:
    return await collect(cipher(frame).seal(stream(payload), key, VERSION))


# --- round trip ------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [b"", b"x", b"hello world", b"y" * (FRAME - 1), b"z" * FRAME, b"w" * (FRAME + 1)],
)
async def test_round_trip(payload: bytes) -> None:
    sealed = await seal(payload)
    assert await collect(cipher().open(stream(sealed), KEY, VERSION)) == payload


async def test_a_multi_frame_payload_round_trips() -> None:
    payload = bytes(range(256)) * 40  # ~10 KB across many frames
    sealed = await seal(payload)
    assert await collect(cipher().open(stream(sealed), KEY, VERSION)) == payload


async def test_the_ciphertext_does_not_contain_the_plaintext() -> None:
    payload = b"TOP-SECRET-MARKER" * 20
    sealed = await seal(payload)
    assert b"TOP-SECRET-MARKER" not in sealed


async def test_sealing_twice_gives_different_ciphertext() -> None:
    """Fresh nonces per frame, so identical content is not identifiable."""
    payload = b"same content"
    assert await seal(payload) != await seal(payload)


async def test_every_frame_uses_a_distinct_nonce() -> None:
    payload = b"a" * (FRAME * 4)
    sealed = await seal(payload)

    nonces = set()
    offset = HEADER_BYTES
    while offset < len(sealed):
        nonces.add(sealed[offset : offset + NONCE_BYTES])
        offset += sealed_frame_bytes(FRAME)

    assert len(nonces) == 4


async def test_the_stored_size_matches_the_predicted_length() -> None:
    payload = b"x" * 3000
    assert len(await seal(payload)) == cipher().ciphertext_length(len(payload))


# --- wrong key or version --------------------------------------------------


async def test_a_different_key_cannot_open_it() -> None:
    sealed = await seal(b"secret")
    with pytest.raises(IntegrityFailureError):
        await collect(cipher().open(stream(sealed), OTHER_KEY, VERSION))


async def test_a_different_version_id_cannot_open_it() -> None:
    """Binding the version id blocks cross-version frame substitution."""
    sealed = await seal(b"secret")
    with pytest.raises(IntegrityFailureError):
        await collect(cipher().open(stream(sealed), KEY, OTHER_VERSION))


# --- tampering -------------------------------------------------------------


async def test_a_flipped_ciphertext_bit_fails_authentication() -> None:
    sealed = bytearray(await seal(b"y" * 500))
    sealed[-1] ^= 0xFF
    with pytest.raises(IntegrityFailureError, match="authentication"):
        await collect(cipher().open(stream(bytes(sealed)), KEY, VERSION))


async def test_a_flipped_nonce_bit_fails_authentication() -> None:
    sealed = bytearray(await seal(b"y" * 500))
    sealed[HEADER_BYTES] ^= 0xFF
    with pytest.raises(IntegrityFailureError):
        await collect(cipher().open(stream(bytes(sealed)), KEY, VERSION))


async def test_reordering_frames_is_detected() -> None:
    """The frame index is authenticated, so a swap cannot go unnoticed."""
    sealed = await seal(b"a" * FRAME + b"b" * FRAME + b"c" * 10)
    size = sealed_frame_bytes(FRAME)
    head = sealed[:HEADER_BYTES]
    first = sealed[HEADER_BYTES : HEADER_BYTES + size]
    second = sealed[HEADER_BYTES + size : HEADER_BYTES + 2 * size]
    tail = sealed[HEADER_BYTES + 2 * size :]

    swapped = head + second + first + tail
    with pytest.raises(IntegrityFailureError):
        await collect(cipher().open(stream(swapped), KEY, VERSION))


async def test_truncation_is_detected() -> None:
    """AEAD alone would accept a prefix; the final-frame marker catches it."""
    sealed = await seal(b"a" * FRAME + b"b" * FRAME + b"c" * 10)
    truncated = sealed[: HEADER_BYTES + sealed_frame_bytes(FRAME)]

    with pytest.raises(IntegrityFailureError, match="truncated"):
        await collect(cipher().open(stream(truncated), KEY, VERSION))


async def test_dropping_only_the_final_frame_is_detected() -> None:
    sealed = await seal(b"a" * FRAME + b"tail")
    truncated = sealed[: HEADER_BYTES + sealed_frame_bytes(FRAME)]

    with pytest.raises(IntegrityFailureError):
        await collect(cipher().open(stream(truncated), KEY, VERSION))


async def test_a_frame_from_another_version_cannot_be_substituted() -> None:
    payload = b"a" * FRAME + b"b" * 10
    mine = await seal(payload)
    theirs = await collect(cipher().seal(stream(payload), KEY, OTHER_VERSION))

    size = sealed_frame_bytes(FRAME)
    spliced = (
        mine[:HEADER_BYTES]
        + theirs[HEADER_BYTES : HEADER_BYTES + size]
        + mine[HEADER_BYTES + size :]
    )

    with pytest.raises(IntegrityFailureError):
        await collect(cipher().open(stream(spliced), KEY, VERSION))


async def test_a_corrupted_header_is_rejected() -> None:
    sealed = b"NOTCFS" + (await seal(b"data"))[6:]
    with pytest.raises(IntegrityFailureError):
        await collect(cipher().open(stream(sealed), KEY, VERSION))


async def test_an_empty_object_is_rejected() -> None:
    with pytest.raises(IntegrityFailureError):
        await collect(cipher().open(stream(b""), KEY, VERSION))


async def test_a_stream_ending_mid_frame_is_rejected() -> None:
    sealed = await seal(b"y" * 500)
    with pytest.raises(IntegrityFailureError):
        await collect(cipher().open(stream(sealed[:-5]), KEY, VERSION))


async def test_errors_never_leak_ciphertext_or_keys() -> None:
    sealed = bytearray(await seal(b"y" * 200))
    sealed[-1] ^= 0xFF
    with pytest.raises(IntegrityFailureError) as exc:
        await collect(cipher().open(stream(bytes(sealed)), KEY, VERSION))

    message = str(exc.value)
    assert KEY.hex() not in message
    assert bytes(sealed).hex() not in message
    assert VERSION.hex() not in message


# --- range decryption ------------------------------------------------------


async def decrypt_range(payload: bytes, start: int, length: int) -> bytes:
    sealed = await seal(payload)
    span = frames_for_range(start, length, frame_bytes=FRAME, plaintext_bytes=len(payload))
    offset, size = span.ciphertext_range(FRAME)
    window = sealed[offset : offset + size]
    return await collect(cipher().open(stream(window), KEY, VERSION, span=span))


async def test_a_range_within_one_frame() -> None:
    payload = bytes(range(256)) * 8
    assert await decrypt_range(payload, 10, 20) == payload[10:30]


async def test_a_range_spanning_frames() -> None:
    payload = bytes(range(256)) * 20
    assert await decrypt_range(payload, FRAME - 5, 50) == payload[FRAME - 5 : FRAME + 45]


async def test_a_range_at_the_very_start() -> None:
    payload = b"abcdefghij" * 300
    assert await decrypt_range(payload, 0, 5) == payload[:5]


async def test_a_range_at_the_very_end() -> None:
    payload = b"abcdefghij" * 300
    assert await decrypt_range(payload, len(payload) - 7, 7) == payload[-7:]


async def test_a_range_covering_the_whole_payload() -> None:
    payload = b"abcdefghij" * 300
    assert await decrypt_range(payload, 0, len(payload)) == payload


async def test_a_range_read_fetches_only_the_frames_it_needs() -> None:
    payload = b"x" * (FRAME * 8)
    span = frames_for_range(FRAME * 5, 10, frame_bytes=FRAME, plaintext_bytes=len(payload))
    _, size = span.ciphertext_range(FRAME)

    assert size == sealed_frame_bytes(FRAME), "one frame, not eight"
