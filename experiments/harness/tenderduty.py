"""Tenderduty readings (III-11 option a): its missed-block gauges at the start and the end of a session.

Tenderduty counts from its own start, so one reading alone says nothing; the report compares the
change between a session's start and end readings with the heights whose /commit lacks 48DC2183.
Every gauge carries the labels chain_id, moniker, and name (Tenderduty v2.2.1, docs/prometheus.md).
"""

import re
from pathlib import Path

from harness.adapters import Unreachable
from harness.constants import TENDERDUTY_CHAIN_ID, TENDERDUTY_METRICS_URL
from harness.jsonio import append_jsonl, read_jsonl

READINGS_FILE = "tenderduty.jsonl"
READING_POINTS = ("start", "end")
GAUGES = {
    "missed_blocks": "tenderduty_missed_blocks",
    "signed_blocks": "tenderduty_signed_blocks",
    "proposed_blocks": "tenderduty_proposed_blocks",
    "missed_blocks_for_window": "tenderduty_missed_blocks_for_window",
}
# A labelled sample of the Prometheus text format: name{labels} value. The labels run to the last brace, so a
# brace inside a quoted label value cannot end them early.
SAMPLE_LINE = re.compile(r"(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)\{(?P<labels>.*)\}\s+(?P<value>\S+)")
# One label: name="value", where the value may hold escaped characters such as \".
LABEL_PAIR = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def sample_value(text: str) -> int | float:
    """A sample value as a number; a whole count stays an int."""
    number = float(text)
    if number.is_integer():
        return int(number)
    return number


def parse_chain_gauges(text: str, chain_id: str) -> dict:
    """{metric name: value} of every labelled sample whose chain_id label is chain_id; comments and other chains are skipped."""
    gauges = {}
    for line in text.splitlines():
        match = SAMPLE_LINE.fullmatch(line.strip())
        if match is None:
            continue
        labels = dict(LABEL_PAIR.findall(match["labels"]))
        if labels.get("chain_id") == chain_id:
            gauges[match["name"]] = sample_value(match["value"])
    return gauges


def read_gauges(http) -> dict:
    """The four missed-block gauges of the localnet chain, by reading field.

    ValueError when the endpoint does not answer, answers with an error status, or lacks a gauge.
    """
    try:
        answer = http.request("GET", TENDERDUTY_METRICS_URL, timeout=5.0)
    except Unreachable as error:
        raise ValueError(f"Tenderduty did not answer at {TENDERDUTY_METRICS_URL}: {error}") from error
    if answer.status != 200:
        raise ValueError(f"Tenderduty at {TENDERDUTY_METRICS_URL} answered status {answer.status}")
    found = parse_chain_gauges(answer.body, TENDERDUTY_CHAIN_ID)
    missing = [metric for metric in GAUGES.values() if metric not in found]
    if missing:
        raise ValueError(f"Tenderduty at {TENDERDUTY_METRICS_URL} has no {', '.join(missing)} "
                         f"for chain_id {TENDERDUTY_CHAIN_ID}")
    return {field: found[metric] for field, metric in GAUGES.items()}


def record_tenderduty_reading(env, session, point: str) -> dict:
    """Read the gauges and append them as the session's start or end reading to <session>/tenderduty.jsonl.

    t_ms is the host time just before the request. ValueError for a point other than start or end and for
    every failure of read_gauges; nothing is written then.
    """
    if point not in READING_POINTS:
        raise ValueError(f"the reading point is start or end, not {point}")
    requested_ms = env.clock.now_ms()
    gauges = read_gauges(env.http)
    reading = {"session": session.id, "point": point, "t_ms": requested_ms, **gauges}
    append_jsonl(session.directory / READINGS_FILE, reading)
    return reading


def load_readings(directory: Path) -> list:
    """The readings of a session in file order; a session without the file has none."""
    return read_jsonl(directory / READINGS_FILE)


def reading_pair(readings: list) -> tuple | None:
    """(start, end): the latest start reading and the latest end reading taken after it, or None without both.

    A start reading written by hand (with a source field) counts like a recorded one; extra fields are ignored.
    """
    starts = [reading for reading in readings if reading["point"] == "start"]
    if not starts:
        return None
    start = max(starts, key=lambda reading: reading["t_ms"])
    ends = [reading for reading in readings if reading["point"] == "end" and reading["t_ms"] >= start["t_ms"]]
    if not ends:
        return None
    return start, max(ends, key=lambda reading: reading["t_ms"])
