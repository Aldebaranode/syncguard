"""Verbatim cuts of the copied syncguard logs for the manuscript's code excerpts.

Positions are (time, line number) pairs taken from the file:line sources the analysis
recorded, so an excerpt starts and ends on exactly the marker lines even when several lines
share a millisecond.
"""

from pathlib import Path

from harness.clock import parse_log_time
from harness.constants import BACKUP_NODE, PRIMARY_NODE
from harness.logparse import LINE


def source_bound(t_ms: int, source: str | None) -> tuple:
    """(time, line number) of a log position; a position known only by its time has line number None."""
    return t_ms, int(source.rsplit(":", 1)[1]) if source else None


def raw_log_lines(directory: Path, utc_offset_seconds: int, event_rows: list) -> dict:
    """(time, line number, text) of every timed line of each node's copied log, found through the file labels of the event sources."""
    labels = {}
    for row in event_rows:
        if row["kind"] == "syncguard":
            labels[row["node"]] = row["source"].rsplit(":", 1)[0]
    lines = {}
    for node, label in labels.items():
        path = directory / "logs" / label
        if not path.exists():
            continue
        timed = []
        for number, text in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            match = LINE.match(text)
            if match:
                timed.append((parse_log_time(match["time"], utc_offset_seconds), number, text))
        lines[node] = timed
    return lines


def cut_lines(lines: dict, spans: dict, prefixed: bool) -> str | None:
    """Lines of each node between its start and end positions, merged by time; None when a node's log is missing.

    spans maps a node to (start, end), each a source_bound; a bound without a line number includes every line of that millisecond.
    """
    if any(node not in lines for node in spans):
        return None
    chosen = []
    for order, (node, (start, end)) in enumerate(spans.items()):
        for t_ms, number, text in lines[node]:
            after_start = (t_ms, number) >= (start[0], start[1] or 0)
            before_end = (t_ms, number) <= (end[0], end[1] if end[1] is not None else float("inf"))
            if after_start and before_end:
                chosen.append((t_ms, order, number, f"{node}: {text}" if prefixed else text))
    return "\n".join(text for *_, text in sorted(chosen, key=lambda item: item[:3]))


def last_health_line(lines: dict, node: str, role: str, before_ms: int) -> tuple | None:
    """(time, line number, text) of the node's last health-status line of the role before an instant."""
    tag = f"[{role}] height="
    found = [line for line in lines.get(node, []) if line[0] < before_ms and tag in line[2]]
    return found[-1] if found else None


def excerpt_for(row: dict, failback: dict, lines: dict, log_events: list) -> dict | None:
    """The detection, failover, and failback excerpts of one iteration, or None when one cannot be cut."""
    initiated = next((event for event in log_events if event.marker == "failover_initiated" and event.node == PRIMARY_NODE
                      and event.t_ms >= row["t_fault_ms"]), None)
    passive = next((event for event in log_events if event.marker == "failover_complete" and event.node == PRIMARY_NODE
                    and event.t_ms >= row["t_fault_ms"]), None)
    sources, back_sources = row["sources"], failback["sources"]
    if (initiated is None or passive is None or row["t_takeover_ms"] is None or "first_failed_check" not in sources
            or "detect" not in sources or "takeover" not in sources or "failback_considered" not in back_sources
            or "failback_complete" not in back_sources):
        return None
    considered = source_bound(failback["t_failback_considered_ms"], back_sources["failback_considered"])
    complete = source_bound(failback["t_failback_complete_ms"], back_sources["failback_complete"])
    released = (source_bound(failback["t_release_complete_ms"], back_sources["release_complete"])
                if "release_complete" in back_sources else (complete[0], None))
    excerpts = {
        "detection": cut_lines(lines, {PRIMARY_NODE: (source_bound(row["t_first_failed_check_ms"], sources["first_failed_check"]),
                                                      source_bound(row["t_detect_ms"], sources["detect"]))}, False),
        "failover": cut_lines(lines, {
            PRIMARY_NODE: (source_bound(initiated.t_ms, initiated.source), source_bound(passive.t_ms, passive.source)),
            BACKUP_NODE: ((initiated.t_ms, None), source_bound(row["t_takeover_ms"], sources["takeover"]))}, True),
        "failback": cut_lines(lines, {PRIMARY_NODE: (considered, complete), BACKUP_NODE: ((considered[0], None), released)}, True),
    }
    return None if None in excerpts.values() else excerpts
