"""Parse syncguard log lines (logrus text format) into timed events that keep their file:line source."""

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from harness.clock import parse_log_time

LINE = re.compile(r'^time="(?P<time>[^"]+)" level=(?P<level>\w+) msg="(?P<msg>(?:[^"\\]|\\.)*)"(?P<rest>.*)$')
FIELD = re.compile(r'(?P<key>[\w-]+)=(?P<value>"(?:[^"\\]|\\.)*"|\S+)')
HEALTH_STATUS = re.compile(
    r"^\[(?P<role>active|passive)\] height=(?P<height>\d+) peers=(?P<peers>\d+) healthy=(?P<healthy>true|false)$")
CONTAINER_RESTARTED = re.compile(r"^Container (?P<container>\S+) restarted$")
TRANSITION_REFUSED = re.compile(r"^Refused (?P<transition>.+?) with (?P<status>\d+) (?P<code>\S+): (?P<cause>.*)$")
# internal/server/server.go acceptNotify (11bbc8f): "Rejected unauthenticated %s notify request from %s"
NOTIFY_REJECTED = re.compile(r"^Rejected unauthenticated (?P<endpoint>\S+) notify request from (?P<remote>\S+)$")
LOCK_ERROR_TEXT = "state is already locked"  # internal/state/manager.go AcquireLock

# Message prefixes copied from the Go sources named beside each one.
PREFIX_MARKERS = (
    ("manager_starting", "Starting failover manager - "),  # internal/manager/failover.go Start
    ("manager_started", "SyncGuard failover manager started"),  # cli/cmd/cmd.go runRootCommand
    ("shutdown_signal", "Received signal "),  # cli/cmd/cmd.go waitForShutdown
    ("health_failed", "CometBFT health check failed: "),  # internal/health/checker.go
    ("warmup_ignored", "In post-restart warm-up, ignoring health-check failure"),  # failover.go
    ("max_failures", "Maximum failures reached, initiating failover"),  # failover.go handleHealthCheckFailure
    ("failover_initiated", "Initiating failover - releasing validator duties"),  # failover.go initiateFailover
    ("key_transferred", "Successfully transferred validator key to peer"),  # failover.go transferKeyToPeer
    ("key_received", "Successfully received and staged validator key"),  # internal/server/server.go
    ("key_received", "Successfully received and saved validator key"),  # server.go wording before 5cfa039
    ("restart_failed", "Failed to restart node onto the mock key: "),  # failover.go initiateFailover
    ("stopped_after_failed_restart", "Node stopped after the failed restart; handing over to the peer"),
    ("failover_aborted", "Aborting failover, staying active: "),  # failover.go initiateFailover
    ("peer_no_takeover", "Peer did not take over; validator offline until failback: "),  # failover.go
    ("failover_complete", "Failover complete - node is now passive"),  # failover.go initiateFailover
    ("failover_notified", "Received failover notification from peer"),  # server.go handleFailoverNotify
    ("takeover_started", "Taking over validator duties"),  # internal/manager/transition.go TakeOver
    ("warmup_entered", "Entering post-restart warm-up for "),  # failover.go BeginWarmup
    ("takeover_complete", "Successfully took over as active validator"),  # transition.go TakeOver
    ("takeover_refused_unhealthy", "Refusing takeover: local node is not healthy"),  # transition.go TakeOver
    ("takeover_node_still_running",
     "Node still running after failed takeover restart; it may be signing with the validator key"),
    ("restarting_container", "Restarting container: "),  # internal/node/docker.go Restart
    ("failback_considered", "Primary node healthy, initiating failback"),  # failover.go considerFailback
    ("failback_initiated", "Initiating failback to primary"),  # failover.go initiateFailback
    ("key_retrieved", "Successfully retrieved validator key from peer"),  # failover.go requestKeyFromPeer
    ("failback_aborted", "Peer did not release validator duties, aborting failback: "),  # failover.go
    ("failback_kept_key",
     "Peer released validator duties; keeping validator key for the next failback attempt: "),
    ("failback_complete", "Failback complete - node is now active"),  # failover.go initiateFailback
    ("failback_notified", "Received failback notification from peer"),  # server.go handleFailbackNotify
    ("release_started", "Releasing validator duties for failback"),  # transition.go ReleaseForFailback
    ("release_complete", "Successfully released validator duties"),  # transition.go ReleaseForFailback
    ("unauthenticated_rejected", "Rejected unauthenticated validator key request from "),  # server.go
)
LEGACY_MARKER_TEXTS = {"Successfully received and saved validator key"}
# Markers that show a node changed role or took a key; the one definition shared by preconditions and security.
ROLE_CHANGE_MARKERS = ("failover_notified", "takeover_started", "takeover_complete", "key_received", "failback_complete")


@dataclass(frozen=True)
class LogEvent:
    """One recognised syncguard log line: what happened, where, when, and its file:line source."""

    marker: str
    node: str
    t_ms: int
    level: str
    message: str
    source: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Plain dict form for events.jsonl."""
        return asdict(self)


def classify(message: str) -> tuple | None:
    """Name the event a log message records, with its parsed details, or None for routine lines."""
    health = HEALTH_STATUS.match(message)
    if health:
        return "health_status", {
            "role": health["role"],
            "height": int(health["height"]),
            "peers": int(health["peers"]),
            "healthy": health["healthy"] == "true",
        }
    restarted = CONTAINER_RESTARTED.match(message)
    if restarted:
        return "container_restarted", {"container": restarted["container"]}
    refused = TRANSITION_REFUSED.match(message)
    if refused:
        return "transition_refused", {
            "transition": refused["transition"],
            "status": int(refused["status"]),
            "code": refused["code"],
        }
    rejected = NOTIFY_REJECTED.match(message)
    if rejected:
        return "notify_rejected", {"endpoint": rejected["endpoint"]}
    for marker, prefix in PREFIX_MARKERS:
        if message.startswith(prefix):
            tail = message[len(prefix):]
            return marker, ({"detail": tail} if tail else {})
    if LOCK_ERROR_TEXT in message:
        return "lock_error", {}
    return None


def parse_line(line: str, line_number: int, file_label: str, default_node: str,
               utc_offset_seconds: int) -> LogEvent | None:
    """Parse one log line into a LogEvent, or None when it is not a recognised syncguard event."""
    match = LINE.match(line.rstrip("\n"))
    if match is None:
        return None
    message = match["msg"].replace('\\"', '"')
    classified = classify(message)
    if classified is None:
        return None
    marker, details = classified
    if LOCK_ERROR_TEXT in message:
        details["lock_error"] = True
    fields = {item["key"]: item["value"].strip('"') for item in FIELD.finditer(match["rest"])}
    return LogEvent(
        marker=marker,
        node=fields.get("node", default_node),
        t_ms=parse_log_time(match["time"], utc_offset_seconds),
        level=match["level"],
        message=message,
        source=f"{file_label}:{line_number}",
        details=details,
    )


def parse_log_file(path: Path, default_node: str, utc_offset_seconds: int,
                   since_ms: int | None = None, until_ms: int | None = None) -> list:
    """Every recognised event of one log file, optionally limited to [since_ms, until_ms]."""
    path = Path(path)
    events = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            event = parse_line(line, number, path.name, default_node, utc_offset_seconds)
            if event is None:
                continue
            if since_ms is not None and event.t_ms < since_ms:
                continue
            if until_ms is not None and event.t_ms > until_ms:
                continue
            events.append(event)
    return events


def first_matching(events: list, where) -> LogEvent | None:
    """Earliest event for which where(event) is true; ties keep their list order."""
    matching = [event for event in events if where(event)]
    if not matching:
        return None
    return min(matching, key=lambda event: event.t_ms)


def first_event(events: list, marker: str, node: str | None = None, after_ms: int | None = None,
                where=None) -> LogEvent | None:
    """Earliest event with this marker, optionally on one node, at or after after_ms, and meeting where."""

    def wanted(event: LogEvent) -> bool:
        """Whether one event satisfies every given condition."""
        if event.marker != marker:
            return False
        if node is not None and event.node != node:
            return False
        if after_ms is not None and event.t_ms < after_ms:
            return False
        return where is None or where(event)

    return first_matching(events, wanted)


def event_from_dict(data: dict) -> LogEvent:
    """Rebuild a LogEvent from its events.jsonl row; extra keys such as session are ignored."""
    return LogEvent(
        marker=data["marker"],
        node=data["node"],
        t_ms=data["t_ms"],
        level=data["level"],
        message=data["message"],
        source=data["source"],
        details=data.get("details", {}),
    )
