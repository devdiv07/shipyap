"""Posting slots: the owner's weekly times (for example Mon, Wed and Fri at 09:30 in India),
turned into the exact UTC time an approval needs. Pure; no clock, file or network access."""

from datetime import UTC, datetime, timedelta, timezone

from public_research_os.publication.approval import MIN_LEAD

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def owner_zone(offset: str) -> timezone:
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = int(offset[1:3]), int(offset[4:6])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def next_free_slot(
    now: datetime, slots: tuple[str, ...], offset: str, taken: set[str], weeks: int = 8
) -> str | None:
    """The earliest slot at least MIN_LEAD (plus a margin) after `now` whose UTC time is not
    in `taken`. None when every slot in the next `weeks` weeks is taken."""
    zone = owner_zone(offset)
    local_now = now.astimezone(zone)
    start = (local_now - timedelta(days=local_now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    earliest = now + MIN_LEAD + timedelta(minutes=10)
    candidates = []
    for week in range(weeks + 1):
        for slot in slots:
            day, clock = slot.split(" ")
            hour, minute = (int(part) for part in clock.split(":"))
            moment = start + timedelta(
                weeks=week, days=_DAYS.index(day), hours=hour, minutes=minute
            )
            candidates.append(moment.astimezone(UTC))
    for moment in sorted(candidates):
        text = moment.strftime(UTC_FORMAT)
        if moment >= earliest and text not in taken:
            return text
    return None


def describe_local(utc_text: str, offset: str) -> str:
    """'Mon 2 Nov 2026, 09:30' in the owner's clock, for the review page."""
    moment = datetime.strptime(utc_text, UTC_FORMAT).replace(tzinfo=UTC)
    local = moment.astimezone(owner_zone(offset))
    return f"{_DAYS[local.weekday()]} {local.day} {local.strftime('%b %Y, %H:%M')}"
