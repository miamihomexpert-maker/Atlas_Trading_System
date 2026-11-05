from datetime import datetime, time, timezone
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
