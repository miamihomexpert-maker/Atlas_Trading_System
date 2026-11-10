from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _new_york():
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as error:
        raise RuntimeError(
            "America/New_York time zone data is unavailable. Install the tzdata package."
        ) from error


def regular_session_status(now=None):
    """Return (is_open, reason) for the US regular session.

    The session is 09:30 inclusive through 16:00 exclusive, America/New_York,
    Monday through Friday. Holidays are not removed.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone.")
    eastern = current.astimezone(_new_york())
    if eastern.weekday() >= 5:
        return False, "weekend"
    clock = eastern.timetz().replace(tzinfo=None)
    if clock < time(9, 30):
        return False, "before_open"
    if clock >= time(16, 0):
        return False, "after_close"
    return True, "open"


def session_clock(now=None):
    """Describe the regular session and the next open or close.

    Holidays are not marked. A weekday inside 09:30–16:00 America/New_York
    is reported as open even when an exchange holiday falls on that day.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone.")
    eastern = current.astimezone(_new_york())
    is_open, reason = regular_session_status(current)
    if reason == "open":
        next_at = _at_clock(eastern, 16, 0)
        next_event = "close"
    elif reason == "before_open":
        next_at = _at_clock(eastern, 9, 30)
        next_event = "open"
    else:
        next_at = _next_open(eastern)
        next_event = "open"
    labels = {
        "weekend": "Weekend",
        "before_open": "Before the open",
        "open": "Regular session is open",
        "after_close": "After the close",
    }
    return {
        "timezone": "America/New_York",
        "as_of": eastern.isoformat(),
        "time_label": eastern.strftime("%Y-%m-%d %H:%M:%S ET"),
        "status": reason,
        "status_label": labels[reason],
        "is_open": is_open,
        "holiday_note": (
            "Exchange holidays are not marked. This clock only knows weekdays "
            "and the regular session, 09:30 to 16:00 America/New_York."
        ),
        "next_event": next_event,
        "next_event_at": next_at.isoformat(),
        "seconds_until_next": int((next_at - eastern).total_seconds()),
    }


def as_new_york(now):
    """Convert an aware datetime to America/New_York."""
    if now.tzinfo is None:
        raise ValueError("now must include a timezone.")
    return now.astimezone(_new_york())


def seconds_until_regular_close(now):
    """Seconds until 16:00 America/New_York, when that close is still ahead today.

    Returns None on a weekend and when the clock is already at or after the close.
    """
    eastern = as_new_york(now)
    if eastern.weekday() >= 5:
        return None
    remaining = (_at_clock(eastern, 16, 0) - eastern).total_seconds()
    if remaining <= 0:
        return None
    return remaining


def _at_clock(eastern, hour, minute):
    return eastern.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _next_open(eastern):
    candidate = eastern
    for _ in range(8):
        if candidate.weekday() < 5:
            opening = _at_clock(candidate, 9, 30)
            if opening > eastern:
                return opening
        candidate = _at_clock(candidate, 0, 0) + timedelta(days=1)
    raise RuntimeError("Could not find the next weekday open.")
