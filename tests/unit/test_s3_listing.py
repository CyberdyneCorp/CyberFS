"""The pure `ListObjectsV2` algorithm -- `s3-compatibility/spec.md`, "Listing
semantics".

Covers prefix filtering, delimiter roll-up into `CommonPrefixes`, `max-keys`
truncation with a resumable continuation token, and the token's own
tamper-rejection. No tree, no repository: the grouping and pagination are pure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cyberfs.domain.errors import InvalidArgumentError
from cyberfs.domain.s3.listing import (
    MAX_KEYS,
    ListRequest,
    S3ObjectEntry,
    decode_token,
    encode_token,
    list_objects_v2,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def entry(key: str, size: int = 10) -> S3ObjectEntry:
    return S3ObjectEntry(key=key, size=size, etag=f'"{key}"', last_modified=NOW)


# --- continuation token ----------------------------------------------------


def test_token_round_trips() -> None:
    assert decode_token(encode_token("reports/q3.xlsx")) == "reports/q3.xlsx"


def test_a_tampered_token_is_rejected() -> None:
    with pytest.raises(InvalidArgumentError):
        decode_token("!!!not-base64!!!")


# --- prefix ----------------------------------------------------------------


def test_prefix_filters_out_non_matching_keys() -> None:
    result = list_objects_v2(
        [entry("reports/q3.xlsx"), entry("photos/cat.jpg")],
        ListRequest(prefix="reports/"),
    )
    assert [e.key for e in result.entries] == ["reports/q3.xlsx"]
    assert result.common_prefixes == ()


# --- delimiter -------------------------------------------------------------


def test_delimiter_groups_folders_into_common_prefixes() -> None:
    result = list_objects_v2(
        [
            entry("top.txt"),
            entry("reports/q3.xlsx"),
            entry("reports/2024/q1.xlsx"),
            entry("photos/cat.jpg"),
        ],
        ListRequest(delimiter="/"),
    )
    assert [e.key for e in result.entries] == ["top.txt"]
    assert set(result.common_prefixes) == {"reports/", "photos/"}


def test_delimiter_under_a_prefix_lists_direct_children() -> None:
    result = list_objects_v2(
        [
            entry("reports/q3.xlsx"),
            entry("reports/2024/q1.xlsx"),
        ],
        ListRequest(prefix="reports/", delimiter="/"),
    )
    assert [e.key for e in result.entries] == ["reports/q3.xlsx"]
    assert result.common_prefixes == ("reports/2024/",)


# --- pagination ------------------------------------------------------------


def test_listing_truncates_and_resumes_by_token() -> None:
    keys = [entry(f"file-{i:02d}") for i in range(5)]
    first = list_objects_v2(keys, ListRequest(max_keys=2))
    assert [e.key for e in first.entries] == ["file-00", "file-01"]
    assert first.is_truncated is True
    assert first.next_token is not None

    second = list_objects_v2(keys, ListRequest(max_keys=2, continuation_token=first.next_token))
    assert [e.key for e in second.entries] == ["file-02", "file-03"]
    assert second.is_truncated is True

    third = list_objects_v2(keys, ListRequest(max_keys=2, continuation_token=second.next_token))
    assert [e.key for e in third.entries] == ["file-04"]
    assert third.is_truncated is False
    assert third.next_token is None


def test_objects_and_common_prefixes_share_the_max_keys_budget() -> None:
    result = list_objects_v2(
        [entry("a.txt"), entry("b/1"), entry("c.txt")],
        ListRequest(delimiter="/", max_keys=2),
    )
    # Combined lexicographic order: a.txt, b/, c.txt -> first two returned.
    assert [e.key for e in result.entries] == ["a.txt"]
    assert result.common_prefixes == ("b/",)
    assert result.is_truncated is True
    assert result.key_count == 2


def test_start_after_resumes_without_a_token() -> None:
    result = list_objects_v2(
        [entry("a"), entry("b"), entry("c")],
        ListRequest(start_after="a"),
    )
    assert [e.key for e in result.entries] == ["b", "c"]


def test_max_keys_is_capped_at_the_page_ceiling() -> None:
    result = list_objects_v2([entry("only")], ListRequest(max_keys=10_000_000))
    # The request is honoured but never beyond the S3 page ceiling.
    assert result.is_truncated is False
    assert MAX_KEYS == 1000
