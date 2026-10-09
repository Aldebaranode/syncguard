"""Host clock, timestamp parsing, and timestamp formatting.

Two clocks matter: the macOS host clock (syncguard logs, fault injection) and
the Docker VM clock shared by all containers (CometBFT header and precommit
times). clockoffset.py measures the difference; container_to_host_ms applies it.
"""

import time
from datetime import datetime, timedelta, timezone

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
LOG_TIME_LAYOUTS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def host_now_ms() -> int:
    """Host wall clock as integer epoch milliseconds."""
    return time.time_ns() // 1_000_000


class SystemClock:
    """The host clock; runners take a clock object so tests can pass a fake one."""

    def now_ms(self) -> int:
        """Current host time in epoch milliseconds."""
        return host_now_ms()

    def sleep(self, seconds: float) -> None:
        """Block for the given number of seconds."""
        time.sleep(seconds)


def to_epoch_ms(moment: datetime) -> int:
    """Convert an aware datetime to epoch milliseconds with integer arithmetic."""
    return (moment - EPOCH) // timedelta(milliseconds=1)


def parse_rfc3339_ms(text: str) -> int:
    """Parse a CometBFT RFC 3339 timestamp (up to 9 fraction digits) to epoch ms."""
    return to_epoch_ms(datetime.fromisoformat(text))


def parse_log_time(text: str, utc_offset_seconds: int) -> int:
    """Parse a syncguard log stamp (host local time, ms or whole seconds) to epoch ms."""
    zone = timezone(timedelta(seconds=utc_offset_seconds))
    for layout in LOG_TIME_LAYOUTS:
        try:
            naive = datetime.strptime(text, layout)
        except ValueError:
            continue
        return to_epoch_ms(naive.replace(tzinfo=zone))
    raise ValueError(f"unrecognised log timestamp: {text!r}")


def format_utc(epoch_ms: int) -> str:
    """Render epoch ms as 2026-10-05T02:54:39.620Z."""
    moment = EPOCH + timedelta(milliseconds=epoch_ms)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z"


def format_local(epoch_ms: int, utc_offset_seconds: int) -> str:
    """Render epoch ms in host local time with the syncguard log layout."""
    zone = timezone(timedelta(seconds=utc_offset_seconds))
    moment = (EPOCH + timedelta(milliseconds=epoch_ms)).astimezone(zone)
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{epoch_ms % 1000:03d}"


def format_clock(epoch_ms: int, utc_offset_seconds: int) -> str:
    """Render the local time of day with a decimal comma for the tables: 09:54:39,620."""
    return format_local(epoch_ms, utc_offset_seconds)[11:].replace(".", ",")


def format_clock_seconds(epoch_ms: int, utc_offset_seconds: int) -> str:
    """Render the local time of day to the second for running text: 09:54:39."""
    time_of_day = format_local(epoch_ms, utc_offset_seconds)[11:]
    return time_of_day.split(".")[0]


def host_utc_offset_seconds() -> int:
    """The host's current UTC offset in seconds (WIB is 25200)."""
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        raise RuntimeError("host time zone has no UTC offset")
    return int(offset.total_seconds())


def container_to_host_ms(container_ms: int, offset_ms: float) -> int:
    """Map a container-clock instant onto the host clock: host = container - offset."""
    return round(container_ms - offset_ms)
