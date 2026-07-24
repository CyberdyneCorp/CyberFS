"""A user's own activity: the summary and the feed behind it.

Self-scoped by construction. Every read passes exactly `user.subject`; there is
no argument by which one caller could ask for another's operations. The window
is bounded, so a request can never turn into an unbounded scan.

Service principals never reach this service: they own no tree, and are refused
at the endpoint before a `User` is ever resolved.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from cyberfs.domain.activity import ActivityEntry, ActivityRollup
from cyberfs.domain.auth.policy import utcnow
from cyberfs.domain.errors import ValidationError
from cyberfs.domain.ports.repositories import Page, UnitOfWork
from cyberfs.domain.users import User


class ActivityService:
    def __init__(self, *, max_window_days: int, retention_days: int, page_size_max: int) -> None:
        self._max_window = max_window_days
        self._retention = retention_days
        self._page_size_max = page_size_max

    async def activity(
        self,
        uow: UnitOfWork,
        user: User,
        *,
        window_days: int,
        action: str | None = None,
        limit: int,
        cursor: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ActivityRollup, Page[ActivityEntry]]:
        """The rollup plus a newest-first, paginated feed for one user.

        A window beyond `ACTIVITY_MAX_WINDOW_DAYS` is refused with a validation
        error (422) rather than scanned. The summary is always unfiltered; only
        the feed narrows to `action` when one is given.
        """
        if window_days > self._max_window:
            raise ValidationError(
                "activity window exceeds the maximum",
                window_days=window_days,
                maximum=self._max_window,
            )
        moment = now or utcnow()
        since = moment - timedelta(days=window_days)
        rollup = await uow.activity.summary(actor_subject=user.subject, since=since, until=moment)
        feed = await uow.activity.feed(
            actor_subject=user.subject,
            since=since,
            until=moment,
            action=action,
            limit=min(limit, self._page_size_max),
            cursor=cursor,
        )
        return rollup, feed
