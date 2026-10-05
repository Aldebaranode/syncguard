"""Host-to-container clock offset (R2).

The spec names `date +%s%N`, but the Alpine BusyBox `date` in the node images
ignores %N and prints whole seconds. `busybox adjtimex` without options only
reads the kernel clock and prints it with microseconds, so it is used instead.
"""

import re
import time
from dataclasses import dataclass

ADJTIMEX_METHOD = "docker exec <container> busybox adjtimex (time.tv_sec, time.tv_usec) vs host midpoint"
SECONDS_LINE = re.compile(r"time\.tv_sec:\s+(\d+)")
MICROSECONDS_LINE = re.compile(r"time\.tv_usec:\s+(\d+)")


def parse_adjtimex_us(output: str) -> int:
    """Read the kernel time from busybox adjtimex output as epoch microseconds."""
    seconds = SECONDS_LINE.search(output)
    microseconds = MICROSECONDS_LINE.search(output)
    if seconds is None or microseconds is None:
        raise ValueError("adjtimex output has no time.tv_sec and time.tv_usec lines")
    return int(seconds.group(1)) * 1_000_000 + int(microseconds.group(1))


@dataclass(frozen=True)
class ClockOffset:
    """Container-minus-host offset in ms from the fastest sample, its uncertainty bound, and every sample."""

    container: str
    method: str
    offset_ms: float
    offset_uncertainty_ms: float
    samples_ms: list
    round_trips_ms: list


def measure_clock_offset(commands, container: str, samples: int = 5, now_ns=time.time_ns) -> ClockOffset:
    """Take `samples` readings against the host midpoint of each docker exec call.

    The sample with the smallest round trip defines the offset, because its midpoint
    assumption errs by at most half that round trip, which is returned as the uncertainty.
    """
    offsets = []
    round_trips = []
    for _ in range(samples):
        before = now_ns()
        result = commands.run(["docker", "exec", container, "busybox", "adjtimex"], timeout=10)
        after = now_ns()
        if result.exit_code != 0:
            raise RuntimeError(f"docker exec {container} busybox adjtimex failed: {result.stderr.strip()}")
        midpoint_us = (before + after) // 2 // 1000
        offsets.append((parse_adjtimex_us(result.stdout) - midpoint_us) / 1000)
        round_trips.append((after - before) / 1_000_000)
    fastest = round_trips.index(min(round_trips))
    return ClockOffset(container, ADJTIMEX_METHOD, offsets[fastest], round_trips[fastest] / 2, offsets, round_trips)
