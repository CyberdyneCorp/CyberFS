"""Ciphertext framing.

Content is sealed in fixed-size frames rather than as one blob, because a
single AEAD seal over a whole file cannot be decrypted without buffering it and
cannot serve a range at all. Framing gives streaming decryption, range reads at
frame granularity, and -- because the frame index, the version id, and a
final-frame flag are all bound as associated data -- detection of reordering,
truncation, and cross-version substitution.

The arithmetic lives here, separate from the cryptography, so the offset maths
can be tested exhaustively without keys.
"""

from __future__ import annotations

from dataclasses import dataclass

from cyberfs.domain.errors import IntegrityFailureError, ValidationError

#: Identifies the format, so a future change is detectable rather than silently
#: misparsed as garbage.
MAGIC = b"CFSENC"
FORMAT_VERSION = 1
#: `MAGIC(6) || format_version(1) || frame_size(4, big-endian)`
HEADER_BYTES = len(MAGIC) + 1 + 4

NONCE_BYTES = 12
TAG_BYTES = 16
#: Per-frame overhead: a fresh nonce plus the authentication tag.
FRAME_OVERHEAD = NONCE_BYTES + TAG_BYTES

DEFAULT_FRAME_BYTES = 64 * 1024
MIN_FRAME_BYTES = 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024


def encode_header(frame_bytes: int) -> bytes:
    if not MIN_FRAME_BYTES <= frame_bytes <= MAX_FRAME_BYTES:
        raise ValidationError(f"frame size must be between {MIN_FRAME_BYTES} and {MAX_FRAME_BYTES}")
    return MAGIC + bytes([FORMAT_VERSION]) + frame_bytes.to_bytes(4, "big")


def decode_header(header: bytes) -> int:
    """Return the frame size, or raise if this is not our ciphertext."""
    if len(header) < HEADER_BYTES or not header.startswith(MAGIC):
        raise IntegrityFailureError("stored object is not CyberFS ciphertext")
    version = header[len(MAGIC)]
    if version != FORMAT_VERSION:
        raise IntegrityFailureError("unsupported ciphertext format version")
    frame_bytes = int.from_bytes(header[len(MAGIC) + 1 : HEADER_BYTES], "big")
    if not MIN_FRAME_BYTES <= frame_bytes <= MAX_FRAME_BYTES:
        raise IntegrityFailureError("ciphertext declares an implausible frame size")
    return frame_bytes


def associated_data(version_id: bytes, index: int, *, final: bool) -> bytes:
    """What each frame's tag commits to besides its own bytes.

    * `version_id` -- a frame from another version of the same file cannot be
      substituted, even though both were sealed under the same file's key
      lineage.
    * `index` -- frames cannot be reordered.
    * `final` -- the last frame is marked, so a truncated stream is detected
      when it ends without one. AEAD alone would happily accept a prefix.
    """
    return version_id + index.to_bytes(8, "big") + (b"\x01" if final else b"\x00")


def frame_count(plaintext_bytes: int, frame_bytes: int) -> int:
    """How many frames a plaintext of this size occupies.

    Zero-length content still gets one (empty) frame, so every object has a
    final frame to mark and truncation stays detectable.
    """
    if plaintext_bytes <= 0:
        return 1
    return -(-plaintext_bytes // frame_bytes)  # ceiling division


def sealed_frame_bytes(frame_bytes: int) -> int:
    return frame_bytes + FRAME_OVERHEAD


def ciphertext_offset(index: int, frame_bytes: int) -> int:
    """Where frame `index` begins in the stored object."""
    return HEADER_BYTES + index * sealed_frame_bytes(frame_bytes)


def ciphertext_size(plaintext_bytes: int, frame_bytes: int) -> int:
    """Total stored size, including the header and every frame's overhead."""
    full, remainder = divmod(max(plaintext_bytes, 0), frame_bytes)
    body = full * sealed_frame_bytes(frame_bytes)
    # An exact multiple has no partial trailing frame; empty content still has
    # one empty frame, so there is always a final frame to mark.
    if remainder or plaintext_bytes <= 0:
        body += remainder + FRAME_OVERHEAD
    return HEADER_BYTES + body


@dataclass(frozen=True, slots=True)
class FrameSpan:
    """The frames covering a plaintext range, and how to trim them."""

    first_index: int
    last_index: int
    #: Bytes to drop from the front of the first decrypted frame.
    lead_skip: int
    #: How many plaintext bytes the caller actually asked for.
    take: int

    @property
    def frame_count(self) -> int:
        return self.last_index - self.first_index + 1

    def ciphertext_range(self, frame_bytes: int) -> tuple[int, int]:
        """Byte offset and length to fetch from the object store."""
        start = ciphertext_offset(self.first_index, frame_bytes)
        end = ciphertext_offset(self.last_index + 1, frame_bytes)
        return start, end - start


def frames_for_range(
    start: int, length: int, *, frame_bytes: int, plaintext_bytes: int
) -> FrameSpan:
    """Map a plaintext byte range onto the frames that hold it.

    Reading a range therefore fetches only the frames it touches, not the whole
    object -- which is the point of framing in the first place.
    """
    if start < 0 or length <= 0:
        raise ValidationError("range must be a positive span")
    clamped = min(length, max(plaintext_bytes - start, 0))
    if clamped <= 0:
        raise ValidationError("range starts past the end of the content")

    first = start // frame_bytes
    last = (start + clamped - 1) // frame_bytes
    return FrameSpan(
        first_index=first,
        last_index=last,
        lead_skip=start - first * frame_bytes,
        take=clamped,
    )
