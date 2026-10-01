"""Virtual time. Integer nanoseconds. No tz database and no floats."""

from seam.errors import Fault

INT64_MAX = 2**63 - 1


def _civil_from_days(z):
    """Howard Hinnant's civil_from_days. `z` is days since 1970-01-01."""
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    if month <= 2:
        year += 1
    return year, month, day


def format_utc(ns):
    if type(ns) is not int or ns < 0 or ns > INT64_MAX:
        raise Fault("bad_value")
    seconds, nanos = divmod(ns, 1_000_000_000)
    days, sod = divmod(seconds, 86400)
    year, month, day = _civil_from_days(days)
    hour, rem = divmod(sod, 3600)
    minute, sec = divmod(rem, 60)
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{sec:02d}.{nanos:09d}Z"


class VirtualClock:
    def __init__(self, start):
        if type(start) is not int or start < 0 or start > INT64_MAX:
            raise Fault("bad_value")
        self.t = start

    def jump(self, at):
        if type(at) is not int or at < self.t:
            raise Fault("clock_backwards")
        self.t = at
