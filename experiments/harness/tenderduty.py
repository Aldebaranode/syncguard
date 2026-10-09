"""Tenderduty readings (III-11 option a): its missed-block gauges at the start and the end of a session, and its log.

Tenderduty counts from its own start, so one reading alone says nothing; the report compares the
change between a session's start and end readings with the commits of the heights that change
covers. Tenderduty books the commit of height h when block h+1 arrives, so a reading taken at tip T
has counted the commits up to T - 1, and a pair covers the commits of its start tip to its end tip
minus 1. Every gauge carries the labels chain_id, moniker, and name (Tenderduty v2.2.1,
docs/prometheus.md). Its log names every missed block, which lets the report count the misses of one
incident instead of a whole session.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from harness.adapters import Unreachable
from harness.clock import container_to_host_ms
from harness.constants import TENDERDUTY_CHAIN_ID, TENDERDUTY_METRICS_URL, TIP_MARGIN
from harness.jsonio import append_jsonl, read_jsonl
from harness.scenarios.common import collect_heights_in_chunks
from harness.signing import is_absent, load_heights
from harness.waits import wait_until

READINGS_FILE = "tenderduty.jsonl"
# Commits of a pair's range that heights.jsonl lacks; heights.jsonl itself is never written here.
HEIGHTS_FILE = "tenderduty-heights.jsonl"
COMMIT_FILES = ("heights.jsonl", HEIGHTS_FILE)
READING_POINTS = ("start", "end")
RESOLVED_POINT = "start_tip_resolved"  # the tip of a start written by hand, on its own line
READABLE_WAIT_S = 60.0  # how long an end reading waits for the tip to make its last covered commit readable
# A block's header time is the median precommit time of the height before it, so the tip read at an instant lies
# this far below the first height whose header time is at or after that instant.
TIP_BELOW_HEADER = 2
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
TENDERDUTY_LOG = Path("tenderduty") / "tenderduty-run.log"  # in the data directory: the output of the Tenderduty run
# Lines of the Tenderduty v2.2.1 log (td2/ws.go) that name a chain: the start of a block subscription, the progress
# line written at every 20th block, and the warning for a missed block.
WATCH_LINE = re.compile(r"(?P<chain>\S+)\s+watching for NewBlock and Vote events")
PROGRESS_LINE = re.compile(r"🧊 (?P<chain>\S+)\s+block (?P<height>\d+)$")
MISSED_LINE = re.compile(r"missed block (?P<height>\d+) on (?P<chain>\S+)$")


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
    """Append the gauges and the chain tip to <session>/tenderduty.jsonl as the session's start or end reading.

    t_ms is the host time just before the metrics request and tip_height the tip read right after it.
    An end reading then stores the commit of every height its pair covers (complete_commit_range).
    Returns {"reading"}, and for an end reading also {"commit_range"}. ValueError, with nothing
    written, for a point other than start or end, for an end reading without a start reading, and for
    every failure of read_gauges.
    """
    if point not in READING_POINTS:
        raise ValueError(f"the reading point is start or end, not {point}")
    if point == "end" and not any(reading["point"] == "start" for reading in load_readings(session.directory)):
        raise ValueError(f"Sesi {session.id} has no start reading in {READINGS_FILE}: record one with --point start "
                         "or write it by hand before the end reading")
    requested_ms = env.clock.now_ms()
    gauges = read_gauges(env.http)
    reading = {"session": session.id, "point": point, "t_ms": requested_ms, **gauges,
               "tip_height": env.rpc.tip_height()}
    append_jsonl(session.directory / READINGS_FILE, reading)
    if point == "start":
        return {"reading": reading}
    return {"reading": reading, "commit_range": complete_commit_range(env, session)}


def complete_commit_range(env, session) -> dict:
    """Store the commit of every height the session's reading pair covers, its start tip to its end tip minus 1.

    A start written by hand without a tip gets one first (resolve_start_tip). Heights that heights.jsonl
    or tenderduty-heights.jsonl hold already are reused; the others, at both edges or in a gap, are read
    with the chunked height collection into tenderduty-heights.jsonl once the highest of them lies
    TIP_MARGIN below the tip. ValueError when the latest start reading lies after the latest end reading,
    when that end reading has no tip, when its tip is not above the start tip (pair_coverage), when the
    start tip is still unknown after the resolution, or when the chain does not get there within READABLE_WAIT_S.
    """
    readings = load_readings(session.directory)
    pair = reading_pair(readings)
    if pair is None:
        raise ValueError(f"Sesi {session.id}: the latest start reading in {READINGS_FILE} lies after the end reading")
    start, end = pair
    if end.get("tip_height") is None:
        raise ValueError(f"Sesi {session.id}: the latest end reading in {READINGS_FILE} has no tip_height")
    if start_tip(readings, start) is None:
        resolve_start_tip(env, session, start, end["tip_height"])
        readings = load_readings(session.directory)
    coverage = pair_coverage(session.directory, readings, start, end)
    if coverage is None:
        raise ValueError(f"{session.directory / READINGS_FILE}: the start tip of the pair is neither recorded nor "
                         "resolved, so the heights it covers are unknown")
    first, last, missing = coverage
    if missing:
        wait_until_readable(env, missing[-1])
    for run_first, run_last in consecutive_runs(missing):
        collect_heights_in_chunks(env, session, run_first, run_last, file_name=HEIGHTS_FILE)
    return {"first_height": first, "last_height": last, "heights_read": len(missing)}


def resolve_start_tip(env, session, start: dict, end_tip: int) -> int:
    """Append the tip of a start written by hand as a start_tip_resolved line that names the start by its t_ms; return it.

    The tip read at the start's t_ms lies TIP_BELOW_HEADER below the first height whose header time, on
    the host clock, is at or after t_ms. The rule holds to within one height either way, because a block can
    be committed between t_ms and the read of a tip: on the 18 fault_injected stamps of Sesi S5 to S10, whose
    tips were read just before t_ms, it gives the recorded tip for 15 and the tip plus 1 for 3. The
    hand-written line stays as it was written.
    """
    tip = first_height_at_or_after(env.rpc, start["t_ms"], session.offset_ms, end_tip) - TIP_BELOW_HEADER
    append_jsonl(session.directory / READINGS_FILE, {"session": session.id, "point": RESOLVED_POINT,
                                                     "start_t_ms": start["t_ms"], "tip_height": tip,
                                                     "t_ms": env.clock.now_ms()})
    return tip


def first_height_at_or_after(rpc, t_ms: int, offset_ms: float, last: int) -> int:
    """The first height of 1..last whose host-clock header time is at or after t_ms, or last + 1 when none is.

    Header times grow with the height, so a binary search reads about log2(last) blocks.
    """
    low, high = 1, last + 1
    while low < high:
        middle = (low + high) // 2
        if container_to_host_ms(rpc.block_time_ms(middle), offset_ms) >= t_ms:
            high = middle
        else:
            low = middle + 1
    return low


def wait_until_readable(env, height: int) -> None:
    """Wait until height lies TIP_MARGIN below the tip, where its commit holds every late precommit.

    The last height an end reading covers is its own tip minus 1, so the next block is always waited
    for. ValueError when the tip does not get there within READABLE_WAIT_S.
    """

    def readable_tip():
        """The tip once height is readable, else None."""
        tip = env.rpc.tip_height()
        return tip if tip - TIP_MARGIN >= height else None

    if wait_until(readable_tip, env.clock, READABLE_WAIT_S) is None:
        raise ValueError(f"the chain tip did not pass height {height} by {TIP_MARGIN} within {READABLE_WAIT_S:.0f} s, "
                         f"so the commits up to it were not read; the report counts them as heights without commit data")


def without_tenderduty_reads(events: list) -> list:
    """A session's events without the reads of a Tenderduty end reading, whose events name HEIGHTS_FILE.

    An end reading can come hours after the session's runs, so its heights_retry and heights_collected
    events must not end a run window or the session.
    """
    return [event for event in events if event.get("file") != HEIGHTS_FILE]


def consecutive_runs(heights: list) -> list:
    """Ascending heights as (first, last) runs of consecutive heights: [3, 4, 5, 9] gives [(3, 5), (9, 9)]."""
    runs = []
    for height in heights:
        if runs and height == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], height)
        else:
            runs.append((height, height))
    return runs


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


def start_tip(readings: list, start: dict) -> int | None:
    """The tip of a start reading: recorded with it, else resolved later on a start_tip_resolved line; None without either.

    A tip_height of null counts as no recorded tip, like a start written by hand without the key.
    """
    if start.get("tip_height") is not None:
        return start["tip_height"]
    resolved = [reading for reading in readings
                if reading["point"] == RESOLVED_POINT and reading["start_t_ms"] == start["t_ms"]]
    return resolved[0]["tip_height"] if resolved else None


def covered_range(readings: list, start: dict, end: dict) -> tuple | None:
    """(first, last): the heights whose commits the pair's change covers, the start tip to the end tip minus 1.

    None when the start's tip is neither recorded nor resolved, or the end reading has no tip (a
    reading taken before tip_height was recorded, or one whose tip_height is null).
    """
    first = start_tip(readings, start)
    if first is None or end.get("tip_height") is None:
        return None
    return first, end["tip_height"] - 1


def pair_coverage(directory: Path, readings: list, start: dict, end: dict) -> tuple | None:
    """(first, last, missing): the heights the pair covers (covered_range) and those of them without a stored commit.

    missing holds, ascending, the heights of first to last that neither heights.jsonl nor
    tenderduty-heights.jsonl in directory holds. None while the start's or the end's tip is unknown.
    ValueError when the end tip is not above the start tip: that pair covers no height, and its range would
    end before it starts.
    """
    bounds = covered_range(readings, start, end)
    if bounds is None:
        return None
    first, last = bounds
    if last < first:
        raise ValueError(f"{directory / READINGS_FILE}: the end tip {end['tip_height']} is not above the start tip "
                         f"{first}, so the pair covers no height")
    return first, last, heights_without_commit(stored_commits(directory), first, last)


def stored_commits(directory: Path) -> dict:
    """{height: HeightRecord} of heights.jsonl and tenderduty-heights.jsonl; a height that both hold is taken once."""
    commits = {}
    for name in COMMIT_FILES:
        for record in load_heights(directory / name):
            commits.setdefault(record.height, record)
    return commits


def heights_without_commit(commits: dict, first: int, last: int) -> list:
    """The heights of first to last that commits ({height: HeightRecord}) does not hold, ascending."""
    return [height for height in range(first, last + 1) if height not in commits]


def incident_counts(commits: dict, missed: frozenset, start: int, end: int, validator: str) -> dict:
    """Tenderduty's logged misses beside the chain's absent heights over one incident, start to end - 1 (III-11).

    Tenderduty v2.2.1 judges block h by its LastCommit, the commit of h - 1 (td2/ws.go, handleBlocks), so
    the commits of start to end - 1 are the logged blocks start + 1 to end. It finds the validator in a
    LastCommit with any block_id_flag, so a nil precommit is present on both sides (signing.is_absent).
    It reports a block the validator proposed as proposed whatever its LastCommit holds, so an absent
    height whose next block the validator proposed is counted apart (absent_on_proposed), and difference
    is the logged count minus the absent heights Tenderduty can see. The chain counts and difference stay
    None while a height of start to end has no commit (missing_heights): the commits of start to end - 1,
    and the header of end, which names the proposer of the last block the log reports.
    """
    logged = sum(1 for height in missed if start + 1 <= height <= end)
    missing = heights_without_commit(commits, start, end)
    counts = {"logged_missed": logged, "missing_heights": len(missing), "chain_absent": None,
              "absent_on_proposed": None, "difference": None}
    if missing:
        return counts
    absent = [height for height in range(start, end) if is_absent(commits[height], validator)]
    on_proposed = [height for height in absent if commits[height + 1].proposer == validator]
    counts.update(chain_absent=len(absent), absent_on_proposed=len(on_proposed),
                  difference=logged - (len(absent) - len(on_proposed)))
    return counts


@dataclass(frozen=True)
class TenderdutyLog:
    """The localnet chain as Tenderduty's log reports it: the blocks it logged as missed and the blocks it watched.

    Each block subscription starts with a 'watching for NewBlock and Vote events' line and judges every
    block it receives. The log names every 20th block and every missed one, so a subscription has judged
    every block from the first to the last height it names; watched holds those (first, last) spans.
    reason says why the log was not read, and then missed and watched are empty.
    """

    missed: frozenset
    watched: tuple
    reason: str | None = None

    def covers(self, first: int, last: int) -> bool:
        """Whether one subscription judged every block from first to last."""
        return any(low <= first and last <= high for low, high in self.watched)

    def outside_subscriptions(self, first: int, last: int) -> int | None:
        """How many blocks of first to last lie outside every subscription span, or None when the log was not read.

        Tenderduty v2.2.1 judges only the blocks its websocket delivers and never fetches the ones that arrived
        while it was down (td2/ws.go, WsRun). A span runs from the first to the last block its subscription
        names, so up to 19 judged blocks at each end of a subscription count as outside it: the count can
        exceed the blocks no subscription judged, never fall short of them.
        """
        if self.reason is not None:
            return None
        return sum(1 for block in range(first, last + 1) if not self.covers(block, block))


def read_tenderduty_log(path: Path | None) -> TenderdutyLog:
    """The missed blocks and the watched spans of the localnet chain (TENDERDUTY_CHAIN_ID) in Tenderduty's log.

    Lines of other kinds and of other chains are ignored, and so is every line before the first subscription
    line: no subscription in the log judged a block named there, so it is neither missed nor in a span.
    Without a path (no data directory) or a file at it, the log is empty with the reason.
    """
    if path is None or not path.exists():
        return TenderdutyLog(frozenset(), (), f"{TENDERDUTY_LOG} was not found in the data directory")
    missed = set()
    subscription_heights = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if chain_match(WATCH_LINE, line) is not None:
            subscription_heights.append([])
            continue
        if not subscription_heights:
            continue
        missed_line = chain_match(MISSED_LINE, line)
        if missed_line is not None:
            missed.add(int(missed_line["height"]))
        judged = missed_line if missed_line is not None else chain_match(PROGRESS_LINE, line)
        if judged is not None:
            subscription_heights[-1].append(int(judged["height"]))
    watched = tuple((min(heights), max(heights)) for heights in subscription_heights if heights)
    return TenderdutyLog(frozenset(missed), watched)


def chain_match(pattern: re.Pattern, line: str) -> re.Match | None:
    """The match of pattern in line when the chain it names is the localnet chain, else None."""
    match = pattern.search(line)
    if match is None or match["chain"] != TENDERDUTY_CHAIN_ID:
        return None
    return match
