"""Host-to-container clock offset (R2).

The spec names `date +%s%N`, but the Alpine BusyBox `date` in the node images
ignores %N and prints whole seconds. `busybox adjtimex` without options only
reads the kernel clock and prints it with microseconds, so it is used instead.
"""

import re
import statistics
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
    """Median container-minus-host offset in ms, with every sample and docker exec round trip."""

    container: str
    method: str
    offset_ms: float
    samples_ms: list
    round_trips_ms: list


def measure_clock_offset(commands, container: str, samples: int = 5, now_ns=time.time_ns) -> ClockOffset:
    """Take `samples` readings, each against the host midpoint around its docker exec call, and keep the median."""
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
    return ClockOffset(container, ADJTIMEX_METHOD, statistics.median(offsets), offsets, round_trips)
