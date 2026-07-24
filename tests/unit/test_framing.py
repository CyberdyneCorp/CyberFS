"""Ciphertext frame arithmetic -- no keys involved."""

from __future__ import annotations

import pytest

from cyberfs.domain.errors import IntegrityFailureError, ValidationError
from cyberfs.domain.framing import (
    FRAME_OVERHEAD,
    HEADER_BYTES,
    MAGIC,
    MAX_FRAME_BYTES,
    MIN_FRAME_BYTES,
    associated_data,
    ciphertext_offset,
    ciphertext_size,
    decode_header,
    encode_header,
    frame_count,
    frames_for_range,
    sealed_frame_bytes,
)

FRAME = 1024


# --- header ----------------------------------------------------------------


def test_header_round_trips() -> None:
    assert decode_header(encode_header(FRAME)) == FRAME


def test_header_is_fixed_width() -> None:
    assert len(encode_header(FRAME)) == HEADER_BYTES


def test_header_carries_the_magic() -> None:
    assert encode_header(FRAME).startswith(MAGIC)


@pytest.mark.parametrize("size", [0, MIN_FRAME_BYTES - 1, MAX_FRAME_BYTES + 1])
def test_implausible_frame_sizes_are_refused(size: int) -> None:
    with pytest.raises(ValidationError):
        encode_header(size)


def test_foreign_bytes_are_not_our_ciphertext() -> None:
    with pytest.raises(IntegrityFailureError, match="not CyberFS ciphertext"):
        decode_header(b"PK\x03\x04\x00\x00\x00\x00\x00\x00\x00")


def test_a_truncated_header_is_refused() -> None:
    with pytest.raises(IntegrityFailureError):
        decode_header(MAGIC)


def test_an_unknown_format_version_is_refused() -> None:
    """A format change must be detected, not silently misparsed."""
    header = bytearray(encode_header(FRAME))
    header[len(MAGIC)] = 99
    with pytest.raises(IntegrityFailureError, match="format version"):
        decode_header(bytes(header))


def test_an_implausible_declared_frame_size_is_refused() -> None:
    header = MAGIC + bytes([1]) + (10).to_bytes(4, "big")
    with pytest.raises(IntegrityFailureError, match="frame size"):
        decode_header(header)


# --- associated data -------------------------------------------------------


def test_associated_data_distinguishes_frame_indices() -> None:
    version = b"\x01" * 16
    assert associated_data(version, 0, final=False) != associated_data(version, 1, final=False)


def test_associated_data_distinguishes_versions() -> None:
    assert associated_data(b"\x01" * 16, 0, final=False) != associated_data(
        b"\x02" * 16, 0, final=False
    )


def test_associated_data_distinguishes_the_final_frame() -> None:
    """What makes truncation detectable at all."""
    version = b"\x01" * 16
    assert associated_data(version, 3, final=True) != associated_data(version, 3, final=False)


# --- sizes -----------------------------------------------------------------


def test_empty_content_still_has_one_frame() -> None:
    """So there is always a final frame to mark."""
    assert frame_count(0, FRAME) == 1


@pytest.mark.parametrize(
    ("plaintext", "expected"),
    [(1, 1), (FRAME - 1, 1), (FRAME, 1), (FRAME + 1, 2), (FRAME * 3, 3), (FRAME * 3 + 1, 4)],
)
def test_frame_count(plaintext: int, expected: int) -> None:
    assert frame_count(plaintext, FRAME) == expected


def test_ciphertext_size_of_empty_content() -> None:
    assert ciphertext_size(0, FRAME) == HEADER_BYTES + FRAME_OVERHEAD


def test_ciphertext_size_of_a_partial_frame() -> None:
    assert ciphertext_size(100, FRAME) == HEADER_BYTES + 100 + FRAME_OVERHEAD


def test_ciphertext_size_of_an_exact_multiple() -> None:
    assert ciphertext_size(FRAME * 2, FRAME) == HEADER_BYTES + 2 * sealed_frame_bytes(FRAME)


def test_ciphertext_grows_with_the_plaintext() -> None:
    sizes = [ciphertext_size(n, FRAME) for n in (0, 100, FRAME, FRAME * 2)]
    assert sizes == sorted(sizes)


def test_overhead_is_small_relative_to_content() -> None:
    payload = 64 * 1024 * 100
    overhead = ciphertext_size(payload, 64 * 1024) - payload
    assert overhead / payload < 0.001


def test_frame_offsets_are_uniform() -> None:
    assert ciphertext_offset(0, FRAME) == HEADER_BYTES
    assert ciphertext_offset(1, FRAME) == HEADER_BYTES + sealed_frame_bytes(FRAME)
    assert ciphertext_offset(3, FRAME) == HEADER_BYTES + 3 * sealed_frame_bytes(FRAME)


# --- range mapping ---------------------------------------------------------


def span(start: int, length: int, total: int = FRAME * 4):
    return frames_for_range(start, length, frame_bytes=FRAME, plaintext_bytes=total)


def test_a_range_inside_one_frame() -> None:
    result = span(10, 20)
    assert (result.first_index, result.last_index) == (0, 0)
    assert (result.lead_skip, result.take) == (10, 20)


def test_a_range_spanning_two_frames() -> None:
    result = span(FRAME - 5, 10)
    assert (result.first_index, result.last_index) == (0, 1)
    assert result.frame_count == 2


def test_a_range_starting_at_a_frame_boundary() -> None:
    result = span(FRAME, 10)
    assert result.first_index == 1
    assert result.lead_skip == 0


def test_a_range_past_the_end_is_clamped() -> None:
    result = span(FRAME * 4 - 10, 500)
    assert result.take == 10


def test_a_range_starting_past_the_end_is_refused() -> None:
    with pytest.raises(ValidationError):
        span(FRAME * 10, 10)


@pytest.mark.parametrize(("start", "length"), [(-1, 10), (0, 0), (0, -5)])
def test_a_nonsensical_range_is_refused(start: int, length: int) -> None:
    with pytest.raises(ValidationError):
        span(start, length)


def test_the_fetched_ciphertext_covers_exactly_the_needed_frames() -> None:
    """A range read must not pull the whole object."""
    result = span(FRAME + 100, 50)
    offset, length = result.ciphertext_range(FRAME)

    assert offset == ciphertext_offset(1, FRAME)
    assert length == sealed_frame_bytes(FRAME)


def test_a_whole_file_range_covers_every_frame() -> None:
    result = span(0, FRAME * 4)
    offset, length = result.ciphertext_range(FRAME)

    assert offset == HEADER_BYTES
    assert length == 4 * sealed_frame_bytes(FRAME)
