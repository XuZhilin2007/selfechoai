from __future__ import annotations

from datetime import date, datetime, time, timezone

from app.schemas import DashboardItem, PersonalItemPublic, PriorityLevel


# Kept in one small module so later experiments can change weights or mappings safely.
URGENCY_WEIGHT = 0.65
IMPORTANCE_WEIGHT = 0.35
LEVEL_VALUE = {
    PriorityLevel.HIGH: 3,
    PriorityLevel.MEDIUM: 2,
    PriorityLevel.LOW: 1,
}


def calculate_priority_score(item: PersonalItemPublic) -> float | None:
    """Return a score only when both dimensions are known.

    Unknown is deliberately not mapped to zero or low. Returning None keeps
    incomplete information in a separate dashboard group.
    """

    if item.urgency not in LEVEL_VALUE or item.importance not in LEVEL_VALUE:
        return None
    return round(
        LEVEL_VALUE[item.urgency] * URGENCY_WEIGHT
        + LEVEL_VALUE[item.importance] * IMPORTANCE_WEIGHT,
        2,
    )


def _deadline_timestamp(value: date | datetime | None) -> float:
    if value is None:
        return float("inf")
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.combine(value, time.min)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _dashboard_item(item: PersonalItemPublic, score: float | None) -> DashboardItem:
    return DashboardItem(
        id=item.id,
        title=item.title,
        importance=item.importance,
        urgency=item.urgency,
        deadline=item.deadline,
        estimated_time=item.estimated_time,
        priority_score=score,
    )


def rank_items(
    items: list[PersonalItemPublic],
) -> tuple[list[DashboardItem], list[DashboardItem]]:
    known: list[tuple[PersonalItemPublic, float]] = []
    unknown: list[PersonalItemPublic] = []

    for item in items:
        score = calculate_priority_score(item)
        if score is None:
            unknown.append(item)
        else:
            known.append((item, score))

    # Score descending, then earliest deadline, then the item waiting longest.
    known.sort(
        key=lambda pair: (
            -pair[1],
            _deadline_timestamp(pair[0].deadline),
            pair[0].updated_time.timestamp(),
            pair[0].id,
        )
    )

    # Unknown items never receive a synthetic score. Known urgency is still useful.
    unknown.sort(
        key=lambda item: (
            -LEVEL_VALUE.get(item.urgency, 0),
            _deadline_timestamp(item.deadline),
            -item.updated_time.timestamp(),
            item.id,
        )
    )

    return (
        [_dashboard_item(item, score) for item, score in known],
        [_dashboard_item(item, None) for item in unknown],
    )
