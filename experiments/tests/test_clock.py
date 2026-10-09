"""Timestamp parsing and formatting on the host clock."""

import pytest

from harness.clock import (
    container_to_host_ms,
    format_clock,
    format_clock_seconds,
    format_local,
    format_utc,
    parse_log_time,
    parse_rfc3339_ms,
)

WIB = 7 * 3600


def test_rfc3339_nanoseconds_truncate_to_milliseconds():
    """A CometBFT precommit timestamp with 9 fraction digits."""
    assert parse_rfc3339_ms("2026-10-05T02:54:56.910003755Z") == 1791168896910


def test_rfc3339_eight_and_six_fraction_digits():
    """CometBFT drops trailing zeros, so 8 and 6 digit fractions occur."""
    assert parse_rfc3339_ms("2026-10-05T02:55:56.99410488Z") == 1791168956994
    assert parse_rfc3339_ms("2026-10-05T02:56:16.425293Z") == 1791168976425


def test_log_time_milliseconds_in_wib():
    """A syncguard log stamp is host local time (WIB) with milliseconds."""
    assert parse_log_time("2026-10-05 09:54:39.620", WIB) == 1791168879620


def test_log_time_archived_whole_seconds():
    """Archived logs written before 0cfea46 carry whole seconds."""
    assert parse_log_time("2026-06-18 12:02:36", WIB) == 1781758956000


def test_log_time_rejects_other_layouts():
    """An unknown layout is an error, never a guess."""
    with pytest.raises(ValueError):
        parse_log_time("05/10/2026 09:54", WIB)


def test_format_utc_local_and_clock():
    """UTC ISO form, the syncguard local layout, and the table clock form."""
    assert format_utc(1791168879620) == "2026-10-05T02:54:39.620Z"
    assert format_local(1791168879620, WIB) == "2026-10-05 09:54:39.620"
    assert format_clock(1791168879620, WIB) == "09:54:39,620"


def test_clock_to_the_second():
    """The local time of day without milliseconds, for times listed in running text."""
    assert format_clock_seconds(1791168879620, WIB) == "09:54:39"


def test_container_time_maps_to_host_time():
    """host = container - offset, rounded to whole milliseconds."""
    assert container_to_host_ms(1791168896910, 50.0) == 1791168896860
    assert container_to_host_ms(1791168896910, -2.4) == 1791168896912
