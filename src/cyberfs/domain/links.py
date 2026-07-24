"""Public-link secrets.

Two distinct kinds of secret, hashed for two distinct reasons:

* the **token** is high-entropy and machine-generated, so a plain SHA-256 is
  the right store -- there is nothing to brute-force, and lookups must be a
  single indexed comparison;
* the **passphrase** is human-chosen and therefore weak, so it gets scrypt
  with a per-link salt, and comparison is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: 256 bits, comfortably past the 128-bit floor `sharing/spec.md` requires.
TOKEN_BYTES = 32

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_KEY_BYTES = 32


def generate_token() -> str:
    """A fresh, unguessable link token.

    Encodes no node identifier: the token must reveal nothing about what it
    points at, and must not be derivable from anything an attacker can see.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The stored form. A database leak must not yield working links."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


def hash_passphrase(passphrase: str) -> str:
    """`scrypt$<salt>$<key>`, with a fresh salt per link."""
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    derived = _derive(passphrase, salt)
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_passphrase(passphrase: str, stored: str) -> bool:
    """Constant-time comparison; a malformed stored value simply fails."""
    try:
        scheme, salt_hex, key_hex = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
    except ValueError:
        return False
    return hmac.compare_digest(_derive(passphrase, salt), expected)


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_BYTES,
    )
