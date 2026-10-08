from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


def localdate_in(timezone_name):
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError):
        zone = timezone.get_default_timezone()
    return timezone.localdate(timezone=zone)
