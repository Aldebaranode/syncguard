"""Host-to-container clock offset from busybox adjtimex."""

import pytest

from fakes import FIXTURES, FakeCommands
from harness.clockoffset import measure_clock_offset, parse_adjtimex_us

ADJTIMEX = ("docker", "exec", "validator3-node", "busybox", "adjtimex")


def adjtimex_output(seconds: int, microseconds: int) -> str:
    """The two lines of adjtimex output the parser reads."""
    return f"    time.tv_sec:  {seconds}\n    time.tv_usec: {microseconds}\n"


def test_parse_real_adjtimex_output():
    """The recorded output yields epoch microseconds."""
    text = (FIXTURES / "adjtimex.txt").read_text(encoding="utf-8")
    assert parse_adjtimex_us(text) == 1791173113277148


def test_parse_rejects_output_without_time():
    """Output without the time lines is an error."""
    with pytest.raises(ValueError):
        parse_adjtimex_us("date: unrecognized option")


def test_offset_comes_from_the_minimum_round_trip_sample():
    """Samples 56.246, 50.000 and 0.000 ms with round trips 20, 40 and 10 ms keep sample 3.

    Sample 1: host 915.000 to 915.020 s (midpoint 915.010), container 915.066246 -> 56.246 ms.
    Sample 2: host 916.000 to 916.040 s (midpoint 916.020), container 916.070000 -> 50.000 ms.
    Sample 3: host 917.000 to 917.010 s (midpoint 917.005), container 917.005000 -> 0.000 ms.
    """
    commands = FakeCommands({ADJTIMEX: [
        (0, adjtimex_output(1791172915, 66246), ""),
        (0, adjtimex_output(1791172916, 70000), ""),
        (0, adjtimex_output(1791172917, 5000), ""),
    ]})
    host_ns = iter([
        1791172915_000_000_000, 1791172915_020_000_000,
        1791172916_000_000_000, 1791172916_040_000_000,
        1791172917_000_000_000, 1791172917_010_000_000,
    ])
    offset = measure_clock_offset(commands, "validator3-node", samples=3, now_ns=lambda: next(host_ns))
    assert offset.samples_ms == [56.246, 50.0, 0.0]
    assert offset.offset_ms == 0.0
    assert offset.offset_uncertainty_ms == 5.0
    assert offset.round_trips_ms == [20.0, 40.0, 10.0]
    assert offset.container == "validator3-node"
    assert "adjtimex" in offset.method


def test_offset_fails_when_docker_exec_fails():
    """A failed docker exec is an error, not a zero offset."""
    commands = FakeCommands({ADJTIMEX: [(1, "", "No such container")]})
    with pytest.raises(RuntimeError, match="No such container"):
        measure_clock_offset(commands, "validator3-node", samples=1, now_ns=lambda: 0)


def test_high_latency_outlier_is_not_selected():
    """A slow docker exec skews its midpoint; the fast sample defines the offset and the bound.

    Fast sample: host 100.000 to 100.004 s (midpoint 100.002), container 100.052 -> 50.000 ms, round trip 4 ms.
    Slow sample: host 101.000 to 101.400 s (midpoint 101.200), container 101.350 -> 150.000 ms, round trip 400 ms.
    """
    commands = FakeCommands({ADJTIMEX: [
        (0, adjtimex_output(1791172900, 52000), ""),
        (0, adjtimex_output(1791172901, 350000), ""),
    ]})
    host_ns = iter([
        1791172900_000_000_000, 1791172900_004_000_000,
        1791172901_000_000_000, 1791172901_400_000_000,
    ])
    offset = measure_clock_offset(commands, "validator3-node", samples=2, now_ns=lambda: next(host_ns))
    assert offset.offset_ms == 50.0
    assert offset.offset_uncertainty_ms == 2.0
