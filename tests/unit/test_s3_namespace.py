"""The pure S3 namespace mapping -- `s3-compatibility/spec.md`, "Namespace mapping".

Covers the key<->path mapping in a caller's own tree, the reserved
``shared/<owner>/…`` prefix for nodes shared with them, the round trip back to
a key, and the two refusals that keep bucket names from becoming a user
directory: a foreign bucket is `NoSuchBucket` (identically for every foreign
subject, so existence cannot be probed) and the shared prefix cannot re-address
the caller's own tree.
"""

from __future__ import annotations

import unicodedata

import pytest

from cyberfs.domain.errors import NoSuchBucketError, ValidationError
from cyberfs.domain.s3.namespace import (
    SHARED_PREFIX,
    S3Target,
    bucket_for_subject,
    key_for,
    resolve_key,
    subject_for_bucket,
)

CALLER = "user-alice"
OWNER = "user-bob"


# --- own tree --------------------------------------------------------------


def test_a_key_maps_to_a_path_in_the_callers_tree() -> None:
    target = resolve_key(CALLER, CALLER, "reports/q3.xlsx")
    assert target == S3Target(owner_subject=CALLER, path="/reports/q3.xlsx", via_shared=False)


def test_an_own_key_round_trips() -> None:
    target = resolve_key(CALLER, CALLER, "reports/q3.xlsx")
    assert key_for(CALLER, target.owner_subject, target.path) == "reports/q3.xlsx"


def test_the_bucket_root_maps_to_the_tree_root() -> None:
    assert resolve_key(CALLER, CALLER, "") == S3Target(
        owner_subject=CALLER, path="/", via_shared=False
    )


@pytest.mark.parametrize("key", ["reports/", "/reports", "reports//", "//reports//"])
def test_leading_trailing_and_empty_segments_are_ignored(key: str) -> None:
    # Every spelling of a single `reports` segment addresses `/reports`.
    assert resolve_key(CALLER, CALLER, key).path == "/reports"


def test_the_bucket_is_named_for_the_subject() -> None:
    assert bucket_for_subject(CALLER) == CALLER
    assert subject_for_bucket(CALLER) == CALLER


# --- shared prefix ---------------------------------------------------------


def test_shared_prefix_addresses_another_owners_node() -> None:
    target = resolve_key(CALLER, CALLER, f"{SHARED_PREFIX}/{OWNER}/reports/q3.xlsx")
    assert target == S3Target(owner_subject=OWNER, path="/reports/q3.xlsx", via_shared=True)


def test_a_shared_key_round_trips_with_the_owner_prefix() -> None:
    target = resolve_key(CALLER, CALLER, f"{SHARED_PREFIX}/{OWNER}/reports/q3.xlsx")
    rendered = key_for(CALLER, target.owner_subject, target.path)
    assert rendered == f"{SHARED_PREFIX}/{OWNER}/reports/q3.xlsx"


def test_key_for_a_foreign_node_is_prefixed_but_an_own_node_is_not() -> None:
    assert key_for(CALLER, OWNER, "/reports/q3.xlsx") == f"{SHARED_PREFIX}/{OWNER}/reports/q3.xlsx"
    assert key_for(CALLER, CALLER, "/reports/q3.xlsx") == "reports/q3.xlsx"


def test_the_shared_prefix_requires_an_owner_segment() -> None:
    with pytest.raises(ValidationError):
        resolve_key(CALLER, CALLER, SHARED_PREFIX)


def test_the_shared_prefix_cannot_re_address_your_own_tree() -> None:
    # You reach your own tree without the prefix; allowing both would give one
    # node two keys.
    with pytest.raises(ValidationError):
        resolve_key(CALLER, CALLER, f"{SHARED_PREFIX}/{CALLER}/reports/q3.xlsx")


# --- cross-user isolation --------------------------------------------------


def test_a_foreign_bucket_is_no_such_bucket() -> None:
    with pytest.raises(NoSuchBucketError) as excinfo:
        resolve_key(CALLER, OWNER, "reports/q3.xlsx")
    assert excinfo.value.s3_code == "NoSuchBucket"
    assert excinfo.value.s3_status == 404


def test_two_different_foreign_buckets_fail_identically() -> None:
    """No existence leak: every foreign bucket fails the same way."""
    with pytest.raises(NoSuchBucketError) as one:
        resolve_key(CALLER, "user-bob", "k")
    with pytest.raises(NoSuchBucketError) as two:
        resolve_key(CALLER, "user-carol-who-may-not-exist", "k")
    assert type(one.value) is type(two.value)
    assert one.value.s3_code == two.value.s3_code == "NoSuchBucket"
    assert one.value.message == two.value.message


# --- normalization ---------------------------------------------------------


def test_segments_are_nfc_normalized() -> None:
    # `café` with a combining accent folds to the composed form, exactly as a
    # node name does, so an address and the node it names cannot disagree.
    decomposed = "café/report"
    composed = unicodedata.normalize("NFC", "café")
    target = resolve_key(CALLER, CALLER, decomposed)
    assert target.path == f"/{composed}/report"
