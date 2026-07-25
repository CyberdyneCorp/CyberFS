"""Key wrapping under the master key."""

from __future__ import annotations

import pytest

from cyberfs.adapters.outbound.crypto import (
    KEK_ASSOCIATED_DATA,
    KEY_BYTES,
    NONCE_BYTES,
    MasterKeyProvider,
    master_key_id,
)
from cyberfs.domain.errors import KeyUnavailableError

MASTER_A = b"\x01" * KEY_BYTES
MASTER_B = b"\x02" * KEY_BYTES


def provider(**kw: object) -> MasterKeyProvider:
    return MasterKeyProvider(MASTER_A, **kw)  # type: ignore[arg-type]


# --- construction ----------------------------------------------------------


@pytest.mark.parametrize("bad", [b"", b"short", b"\x00" * 31, b"\x00" * 33])
def test_master_key_must_be_32_bytes(bad: bytes) -> None:
    with pytest.raises(KeyUnavailableError, match="32 bytes"):
        MasterKeyProvider(bad)


def test_previous_master_key_is_also_length_checked() -> None:
    with pytest.raises(KeyUnavailableError, match="previous"):
        MasterKeyProvider(MASTER_A, previous=b"short")


# --- key ids ---------------------------------------------------------------


def test_key_id_is_stable() -> None:
    assert master_key_id(MASTER_A) == master_key_id(MASTER_A)


def test_distinct_keys_get_distinct_ids() -> None:
    assert master_key_id(MASTER_A) != master_key_id(MASTER_B)


def test_key_id_does_not_reveal_the_key() -> None:
    """It is published in rows and logs, so it must be a one-way digest."""
    key_id = master_key_id(MASTER_A)
    assert MASTER_A.hex() not in key_id
    assert len(key_id) == 16


def test_provider_reports_its_current_key_id() -> None:
    assert provider().master_key_id == master_key_id(MASTER_A)


def test_previous_key_id_is_absent_without_rotation() -> None:
    assert provider().previous_master_key_id is None


# --- generation ------------------------------------------------------------


def test_generated_kek_is_256_bits() -> None:
    assert len(provider().generate_kek()) == KEY_BYTES


def test_generated_keks_are_unique() -> None:
    subject = provider()
    assert len({subject.generate_kek() for _ in range(32)}) == 32


# --- wrapping --------------------------------------------------------------


def test_wrap_then_unwrap_round_trips() -> None:
    subject = provider()
    kek = subject.generate_kek()
    wrapped = subject.wrap_kek(kek)
    assert subject.unwrap_kek(wrapped, master_key_id=subject.master_key_id) == kek


def test_wrapped_material_does_not_contain_the_key() -> None:
    subject = provider()
    kek = subject.generate_kek()
    assert kek not in subject.wrap_kek(kek)


def test_each_wrap_uses_a_fresh_nonce() -> None:
    """Nonce reuse under one key would be catastrophic for GCM."""
    subject = provider()
    kek = subject.generate_kek()
    nonces = {subject.wrap_kek(kek)[:NONCE_BYTES] for _ in range(32)}
    assert len(nonces) == 32


def test_wrapping_the_same_key_twice_gives_different_ciphertext() -> None:
    subject = provider()
    kek = subject.generate_kek()
    assert subject.wrap_kek(kek) != subject.wrap_kek(kek)


def test_only_32_byte_keys_are_wrappable() -> None:
    with pytest.raises(KeyUnavailableError, match="32 bytes"):
        provider().wrap_kek(b"too-short")


# --- tampering -------------------------------------------------------------


def test_tampered_ciphertext_fails_authentication() -> None:
    subject = provider()
    wrapped = bytearray(subject.wrap_kek(subject.generate_kek()))
    wrapped[-1] ^= 0xFF
    with pytest.raises(KeyUnavailableError, match="authentication"):
        subject.unwrap_kek(bytes(wrapped), master_key_id=subject.master_key_id)


def test_tampered_nonce_fails_authentication() -> None:
    subject = provider()
    wrapped = bytearray(subject.wrap_kek(subject.generate_kek()))
    wrapped[0] ^= 0xFF
    with pytest.raises(KeyUnavailableError):
        subject.unwrap_kek(bytes(wrapped), master_key_id=subject.master_key_id)


def test_a_different_master_key_cannot_unwrap() -> None:
    wrapped = MasterKeyProvider(MASTER_A).wrap_kek(b"\x09" * KEY_BYTES)
    other = MasterKeyProvider(MASTER_B)
    with pytest.raises(KeyUnavailableError):
        other.unwrap_kek(wrapped, master_key_id=other.master_key_id)


def test_error_never_echoes_key_material() -> None:
    """`content-encryption/spec.md`: no nonces, tags, or ciphertext in errors."""
    subject = provider()
    wrapped = bytearray(subject.wrap_kek(subject.generate_kek()))
    wrapped[-1] ^= 0xFF
    with pytest.raises(KeyUnavailableError) as exc:
        subject.unwrap_kek(bytes(wrapped), master_key_id=subject.master_key_id)
    message = str(exc.value)
    assert bytes(wrapped).hex() not in message
    assert MASTER_A.hex() not in message


def test_associated_data_binds_the_wrap_to_its_purpose() -> None:
    """A KEK-wrapping blob must not be replayable where a DEK is expected."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    subject = provider()
    wrapped = subject.wrap_kek(b"\x09" * KEY_BYTES)
    nonce, sealed = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]

    assert AESGCM(MASTER_A).decrypt(nonce, sealed, KEK_ASSOCIATED_DATA)
    with pytest.raises(InvalidTag):
        AESGCM(MASTER_A).decrypt(nonce, sealed, b"cyberfs/dek/v1")


# --- s3 access-key secrets -------------------------------------------------


def test_seal_secret_round_trips_an_arbitrary_length_value() -> None:
    """Unlike `wrap_kek`, sealing accepts a non-32-byte secret -- the exact
    ~40-character S3 secret must survive unchanged."""
    subject = provider()
    secret = b"eYKibf6yGAHX_PgSjE0a-IP33uBLvuFhIgl9BQ9_"
    sealed = subject.seal_secret(secret)
    assert subject.unseal_secret(sealed, master_key_id=subject.master_key_id) == secret


def test_sealed_secret_does_not_contain_the_plaintext() -> None:
    """Invariant A against real crypto: stored material yields nothing."""
    subject = provider()
    secret = b"top-secret-access-key-value-1234567890AB"
    assert secret not in subject.seal_secret(secret)


def test_each_seal_uses_a_fresh_nonce() -> None:
    subject = provider()
    secret = b"same-secret-every-time"
    ciphertexts = {subject.seal_secret(secret) for _ in range(16)}
    assert len(ciphertexts) == 16


def test_tampered_sealed_secret_fails_authentication() -> None:
    subject = provider()
    sealed = bytearray(subject.seal_secret(b"a-secret"))
    sealed[-1] ^= 0xFF
    with pytest.raises(KeyUnavailableError):
        subject.unseal_secret(bytes(sealed), master_key_id=subject.master_key_id)


def test_sealed_secret_is_not_replayable_as_a_wrapped_kek() -> None:
    """Distinct associated data: a sealed secret and a wrapped KEK never cross."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    subject = provider()
    sealed = subject.seal_secret(b"\x09" * KEY_BYTES)
    nonce, ciphertext = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
    with pytest.raises(InvalidTag):
        AESGCM(MASTER_A).decrypt(nonce, ciphertext, KEK_ASSOCIATED_DATA)


def test_sealed_secret_opens_under_the_previous_key_during_rotation() -> None:
    old = MasterKeyProvider(MASTER_A)
    secret = b"rotate-me"
    sealed = old.seal_secret(secret)
    rotating = MasterKeyProvider(MASTER_B, previous=MASTER_A)
    assert rotating.unseal_secret(sealed, master_key_id=master_key_id(MASTER_A)) == secret


# --- rotation --------------------------------------------------------------


def test_material_sealed_under_the_previous_key_still_opens() -> None:
    """Both keys are accepted while a rotation is in flight."""
    old = MasterKeyProvider(MASTER_A)
    kek = old.generate_kek()
    wrapped = old.wrap_kek(kek)

    rotating = MasterKeyProvider(MASTER_B, previous=MASTER_A)
    assert rotating.unwrap_kek(wrapped, master_key_id=master_key_id(MASTER_A)) == kek


def test_new_material_is_sealed_under_the_current_key_only() -> None:
    rotating = MasterKeyProvider(MASTER_B, previous=MASTER_A)
    kek = rotating.generate_kek()
    wrapped = rotating.wrap_kek(kek)

    assert rotating.unwrap_kek(wrapped, master_key_id=master_key_id(MASTER_B)) == kek
    with pytest.raises(KeyUnavailableError):
        MasterKeyProvider(MASTER_A).unwrap_kek(wrapped, master_key_id=master_key_id(MASTER_A))


def test_rotating_provider_reports_both_ids() -> None:
    rotating = MasterKeyProvider(MASTER_B, previous=MASTER_A)
    assert rotating.master_key_id == master_key_id(MASTER_B)
    assert rotating.previous_master_key_id == master_key_id(MASTER_A)


def test_unknown_master_key_id_is_refused() -> None:
    subject = provider()
    wrapped = subject.wrap_kek(subject.generate_kek())
    with pytest.raises(KeyUnavailableError, match="no master key"):
        subject.unwrap_kek(wrapped, master_key_id="deadbeefdeadbeef")
