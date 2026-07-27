"""Activity rollup and feed value objects.

A user's own operations, summarized and listed. Everything here is pure: the
rollup arithmetic is a fold over grouped counts, testable without a database,
so the SQL adapter contributes only the grouping and the domain owns the
meaning of the numbers.

Two classifications live here because both the summary and the prune job depend
on them:

* `ACTIVITY_ACTIONS` -- the operations that count as activity and that the prune
  job may delete once they age out.
* `SECURITY_ACTIONS` -- everything else (denials, grant changes, ownership
  transfers, encryption changes, admin actions). These are evidence, retained
  under the longer audit retention and never pruned as activity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from cyberfs.domain.audit import AuditAction, AuditProtocol

#: The operations a user's activity is made of. These are what the prune job
#: deletes once they pass `ACTIVITY_RETENTION_DAYS`; nothing else is touched.
ACTIVITY_ACTIONS: frozenset[AuditAction] = frozenset(
    {
        AuditAction.FILE_UPLOADED,
        AuditAction.FILE_DOWNLOADED,
        AuditAction.NODE_CREATED,
        AuditAction.NODE_RENAMED,
        # Labelling a node is an ordinary operation, so these are pruned with the
        # rest of a user's activity rather than retained as security records.
        AuditAction.NODE_TAGS_CHANGED,
        AuditAction.NODE_METADATA_CHANGED,
        AuditAction.NODE_MOVED,
        AuditAction.NODE_COPIED,
        AuditAction.NODE_DELETED,
        AuditAction.NODE_RESTORED,
        AuditAction.VERSION_RESTORED,
        AuditAction.PUBLIC_LINK_ACCESSED,
    }
)

#: Security records: everything that is not activity. Derived so a newly added
#: action is retained by default -- forgetting to classify a new security event
#: keeps it, which is the safe direction.
SECURITY_ACTIONS: frozenset[AuditAction] = frozenset(
    action for action in AuditAction if action not in ACTIVITY_ACTIONS
)

#: Which summary counter each action feeds. Grants are security records (they
#: are retained), yet they still count toward the granter's "shares" totals,
#: because the summary describes what the user *did*, not what is retained.
#:
#: Issuing a public link is sharing, so it counts too. Leaving it out made the
#: summary contradict its own feed: a user who shared only by link saw
#: `shares_granted: 0` beside a list of the links they had just created.
SUMMARY_BUCKETS: dict[AuditAction, str] = {
    AuditAction.FILE_UPLOADED: "uploads",
    AuditAction.FILE_DOWNLOADED: "downloads",
    AuditAction.GRANT_CREATED: "shares_granted",
    AuditAction.GRANT_REVOKED: "shares_revoked",
    AuditAction.PUBLIC_LINK_CREATED: "shares_granted",
    AuditAction.PUBLIC_LINK_REVOKED: "shares_revoked",
    AuditAction.NODE_DELETED: "deletions",
    AuditAction.NODE_RESTORED: "restores",
    AuditAction.VERSION_RESTORED: "restores",
}

#: The counter names, in the order the rollup exposes them.
_BUCKET_NAMES: tuple[str, ...] = (
    "uploads",
    "downloads",
    "shares_granted",
    "shares_revoked",
    "deletions",
    "restores",
)


@dataclass(frozen=True, slots=True)
class ActivityCount:
    """One `(action, day)` bucket from the aggregate query.

    `count` is how many of that action happened on that day; `bytes` is the
    plaintext total for upload and download rows and zero for everything else.
    """

    action: AuditAction
    day: date
    count: int
    bytes: int = 0


@dataclass(frozen=True, slots=True)
class ActivityRollup:
    """A user's activity over a window: counts, byte totals, and busiest day."""

    window_start: datetime
    window_end: datetime
    uploads: int = 0
    downloads: int = 0
    shares_granted: int = 0
    shares_revoked: int = 0
    deletions: int = 0
    restores: int = 0
    bytes_uploaded: int = 0
    bytes_downloaded: int = 0
    busiest_day: date | None = None

    @classmethod
    def empty(cls, window_start: datetime, window_end: datetime) -> ActivityRollup:
        """A quiet account's rollup: all zeroes, no busiest day."""
        return cls(window_start=window_start, window_end=window_end)


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    """One operation in the feed.

    A node the caller does not own carries `node_name = None`: cross-user and
    purged nodes are identified by id alone.
    """

    action: AuditAction
    occurred_at: datetime
    node_id: str | None
    node_name: str | None
    protocol: AuditProtocol = AuditProtocol.REST


def build_rollup(
    counts: Iterable[ActivityCount],
    *,
    window_start: datetime,
    window_end: datetime,
) -> ActivityRollup:
    """Fold grouped `(action, day)` counts into a rollup.

    The busiest day is the day with the most operations of any kind; ties break
    toward the later date so the answer is deterministic.
    """
    buckets = dict.fromkeys(_BUCKET_NAMES, 0)
    per_day: dict[date, int] = {}
    bytes_up = bytes_down = 0

    for entry in counts:
        bucket = SUMMARY_BUCKETS.get(entry.action)
        if bucket is not None:
            buckets[bucket] += entry.count
        if entry.action is AuditAction.FILE_UPLOADED:
            bytes_up += entry.bytes
        elif entry.action is AuditAction.FILE_DOWNLOADED:
            bytes_down += entry.bytes
        per_day[entry.day] = per_day.get(entry.day, 0) + entry.count

    busiest = max(per_day, key=lambda day: (per_day[day], day)) if per_day else None
    return ActivityRollup(
        window_start=window_start,
        window_end=window_end,
        uploads=buckets["uploads"],
        downloads=buckets["downloads"],
        shares_granted=buckets["shares_granted"],
        shares_revoked=buckets["shares_revoked"],
        deletions=buckets["deletions"],
        restores=buckets["restores"],
        bytes_uploaded=bytes_up,
        bytes_downloaded=bytes_down,
        busiest_day=busiest,
    )
