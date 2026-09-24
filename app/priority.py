from __future__ import annotations

from datetime import datetime

from app.schemas import DashboardItem, ItemStatus, PersonalItemPublic
from app.time_utils import deadline_is_overdue, deadline_local_value


def _dashboard_item(item: PersonalItemPublic) -> DashboardItem:
    return DashboardItem(
        id=item.id,
        title=item.title,
        importance=item.importance,
        urgency=item.urgency,
        deadline=item.deadline,
        estimated_time=item.estimated_time,
        status=item.status,
        is_pinned=item.is_pinned,
        completed_at=item.completed_at,
        trashed_at=item.trashed_at,
        status_before_trash=item.status_before_trash,
        priority_score=None,
    )


def rank_items(
    items: list[PersonalItemPublic], *, timezone_name: str, now: datetime,
) -> tuple[list[DashboardItem], list[DashboardItem]]:
    """Rank active items by explicit Pin, then the local Deadline key.

    Precision only orders items within the same calendar day. Date-only values
    never acquire a synthetic clock. Legacy priority fields have no role here.
    """

    def key(item: PersonalItemPublic) -> tuple:
        pin = 0 if item.status == ItemStatus.ACTIVE and item.is_pinned else 1
        created = -item.created_time.timestamp()
        if item.deadline is None:
            return (pin, 2, 0, 0, 0, created, item.id)
        local = deadline_local_value(item.deadline, timezone_name)
        overdue = deadline_is_overdue(
            item.deadline, timezone_name=timezone_name, now=now,
        )
        timed = isinstance(local, datetime)
        day = local.date().toordinal() if timed else local.toordinal()
        # Wall-clock precision, after calendar date; never an epoch timestamp.
        clock = (
            ((local.hour * 60 + local.minute) * 60 + local.second) * 1_000_000
            + local.microsecond
        ) if timed else 0
        return (
            pin,
            0 if overdue else 1,
            -day if overdue else day,
            0 if timed else 1,
            -clock if overdue else clock,
            0 if timed else created,
            item.id,
        )

    return ([_dashboard_item(item) for item in sorted(items, key=key)], [])
