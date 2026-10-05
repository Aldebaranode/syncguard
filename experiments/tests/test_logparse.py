"""syncguard log lines to sourced events."""

from fakes import FIXTURES
from harness.constants import SYNCGUARD_DIR
from harness.logparse import (
    LEGACY_MARKER_TEXTS,
    PREFIX_MARKERS,
    event_from_dict,
    first_event,
    parse_line,
    parse_log_file,
)

WIB = 7 * 3600
ACTIVE = FIXTURES / "logs" / "smoke-active.txt"
PASSIVE = FIXTURES / "logs" / "smoke-passive.txt"


def markers_with_lines(events):
    """(marker, fixture line number) for each event."""
    return [(event.marker, int(event.source.rsplit(":", 1)[1])) for event in events]


def test_active_excerpt_markers_in_order():
    """Routine lines (role banner, Node unhealthy warnings) produce no event."""
    events = parse_log_file(ACTIVE, "validator-1", WIB)
    assert markers_with_lines(events) == [
        ("manager_starting", 1), ("manager_started", 2), ("health_failed", 4), ("health_status", 5),
        ("max_failures", 7), ("failover_initiated", 8), ("key_transferred", 9), ("restarting_container", 10),
        ("container_restarted", 11), ("failover_complete", 12), ("health_status", 13), ("health_status", 15),
        ("health_status", 16), ("failback_considered", 17), ("failback_initiated", 18), ("key_retrieved", 19),
        ("restarting_container", 20), ("container_restarted", 21), ("failback_complete", 22),
        ("health_status", 23), ("warmup_ignored", 24), ("health_status", 25),
    ]


def test_passive_excerpt_markers_in_order():
    """The pre-5cfa039 wording 'received and saved' still maps to key_received."""
    events = parse_log_file(PASSIVE, "validator-2", WIB)
    assert markers_with_lines(events) == [
        ("manager_starting", 1), ("manager_started", 2), ("health_status", 4), ("key_received", 6),
        ("failover_notified", 7), ("takeover_started", 8), ("warmup_entered", 9), ("restarting_container", 10),
        ("container_restarted", 11), ("takeover_complete", 12), ("warmup_ignored", 13), ("health_status", 14),
        ("health_status", 15), ("failback_notified", 16), ("release_started", 17), ("restarting_container", 18),
        ("container_restarted", 19), ("release_complete", 20), ("health_status", 21), ("health_status", 22),
    ]


def test_event_fields_from_a_real_line():
    """Time, level, node, and file:line source of the detection line."""
    detect = first_event(parse_log_file(ACTIVE, "validator-1", WIB), "max_failures")
    assert detect.t_ms == 1791168879620
    assert detect.level == "error"
    assert detect.node == "validator-1"
    assert detect.source == "smoke-active.txt:7"


def test_startup_line_without_node_field_takes_the_file_node():
    """cli/cmd/cmd.go logs the startup line without node=."""
    started = first_event(parse_log_file(PASSIVE, "validator-2", WIB), "manager_started")
    assert started.node == "validator-2"
    assert started.t_ms == 1791168864636


def test_escaped_quotes_are_unescaped():
    """logrus escapes quotes inside msg."""
    failed = first_event(parse_log_file(ACTIVE, "validator-1", WIB), "health_failed")
    assert 'Get "http://localhost:21657/status"' in failed.message


def test_health_status_details():
    """Role, height, peers, and health of a status line."""
    events = parse_log_file(ACTIVE, "validator-1", WIB)
    by_line = {event.source: event for event in events}
    assert by_line["smoke-active.txt:13"].details == {"role": "passive", "height": 0, "peers": 0, "healthy": False}
    assert by_line["smoke-active.txt:15"].details == {"role": "passive", "height": 144, "peers": 5, "healthy": True}


def test_prefix_marker_details():
    """The text after a prefix is kept; restarted lines name the container."""
    events = parse_log_file(PASSIVE, "validator-2", WIB)
    assert first_event(events, "warmup_entered").details == {"detail": "90s"}
    assert first_event(events, "restarting_container").details == {"detail": "validator2-node"}
    assert first_event(events, "container_restarted").details == {"container": "validator2-node"}


def test_transition_refusal_line():
    """Built from server.go 'Refused %s with %d %s: %v' and TransitionError.Error() in errors.go."""
    line = ('time="2026-10-05 10:00:00.000" level=error msg="Refused takeover with 409 not_healthy: '
            'not_healthy: local node is not healthy" module=server node=validator-2')
    event = parse_line(line, 1, "syncguard-passive.log", "validator-2", WIB)
    assert event.marker == "transition_refused"
    assert event.details == {"transition": "takeover", "status": 409, "code": "not_healthy"}


def test_rejected_notify_line():
    """Built from the acceptNotify warning in server.go (11bbc8f)."""
    line = ('time="2026-10-05 11:20:00.000" level=warning msg="Rejected unauthenticated failover notify request '
            'from [::1]:52000" module=server node=validator-2')
    event = parse_line(line, 1, "syncguard-passive.log", "validator-2", WIB)
    assert event.marker == "notify_rejected"
    assert event.details == {"endpoint": "failover"}


def test_archived_lock_error_line():
    """The archived wording (forensics section 7) is flagged as a lock error."""
    line = ('time="2026-07-09 06:14:23" level=error msg="Failed to acquire state lock: state is already locked" '
            'module=server node=validator-1')
    event = parse_line(line, 121984, "syncguard.log", "validator-1", WIB)
    assert event.marker == "lock_error"
    assert event.details == {"lock_error": True}
    assert event.source == "syncguard.log:121984"


def test_archived_second_resolution_line():
    """Archived syncguard.log line 49818."""
    line = ('time="2026-06-18 12:02:36" level=error msg="Maximum failures reached, initiating failover" '
            'module=failover node=validator-1')
    event = parse_line(line, 49818, "syncguard.log", "validator-1", WIB)
    assert event.marker == "max_failures"
    assert event.t_ms == 1781758956000


def test_routine_and_foreign_lines_are_skipped():
    """Lines without a marker, and lines that are not logrus lines, give None."""
    banner = 'time="2026-10-05 09:54:24.617" level=info msg="Node: validator-1, Role: active, Primary: true"'
    assert parse_line(banner, 3, "x", "validator-1", WIB) is None
    assert parse_line("not a log line", 1, "x", "validator-1", WIB) is None


def test_time_window_filter():
    """since and until are inclusive bounds on the event time."""
    events = parse_log_file(ACTIVE, "validator-1", WIB, since_ms=1791168959623, until_ms=1791168961698)
    assert markers_with_lines(events) == [
        ("failback_considered", 17), ("failback_initiated", 18), ("key_retrieved", 19),
        ("restarting_container", 20), ("container_restarted", 21), ("failback_complete", 22),
    ]


def test_first_event_with_node_time_and_condition():
    """The primary's first healthy check after it went passive."""
    events = parse_log_file(ACTIVE, "validator-1", WIB)
    passive = first_event(events, "failover_complete")
    healthy = first_event(events, "health_status", "validator-1", after_ms=passive.t_ms,
                          where=lambda event: event.details["healthy"])
    assert healthy.source == "smoke-active.txt:15"
    assert healthy.t_ms == 1791168899622
    assert first_event(events, "takeover_complete") is None


def test_event_round_trip_through_a_dict():
    """events.jsonl rows rebuild the same event; extra keys are ignored."""
    event = first_event(parse_log_file(PASSIVE, "validator-2", WIB), "takeover_complete")
    row = {**event.to_dict(), "session": "S4", "kind": "syncguard", "iteration": 1}
    assert event_from_dict(row) == event


def test_every_marker_text_exists_in_the_go_sources():
    """Guards against a renamed log message silently breaking the parser."""
    go_files = [path for folder in ("internal", "cli") for path in (SYNCGUARD_DIR / folder).rglob("*.go")
                if not path.name.endswith("_test.go")]
    sources = "\n".join(path.read_text(encoding="utf-8") for path in go_files)
    for _, text in PREFIX_MARKERS:
        if text not in LEGACY_MARKER_TEXTS:
            assert text in sources, text
    for layout in ("Refused %s with %d %s: %v", "Container %s restarted",
                   "[%s] height=%d peers=%d healthy=%v", "state is already locked",
                   "Rejected unauthenticated %s notify request from %s"):
        assert layout in sources, layout
