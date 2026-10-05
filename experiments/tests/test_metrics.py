"""Per-iteration failover and failback metrics from the smoke-test fixtures."""

from dataclasses import asdict, replace

import pytest

from fakes import FIXTURES, smoke_records
from harness.constants import TARGET_VALIDATOR
from harness.logparse import LogEvent, parse_log_file
from harness.metrics import failback_iteration, failover_iteration, seconds_between, summarize_field

WIB = 7 * 3600
FAULT_MS = 1791168828000  # declared test input, 2026-10-05T02:53:48.000Z


def smoke_events():
    """Both fixture logs parsed, as a runner records them."""
    active = parse_log_file(FIXTURES / "logs" / "smoke-active.txt", "validator-1", WIB)
    passive = parse_log_file(FIXTURES / "logs" / "smoke-passive.txt", "validator-2", WIB)
    return active + passive


def smoke_failover():
    """Failover metrics of the fixture iteration."""
    return failover_iteration("SFIX", 1, "cfg", FAULT_MS, "events.jsonl docker kill validator1-node",
                              smoke_events(), smoke_records(), TARGET_VALIDATOR)


def test_seconds_between():
    """Milliseconds to seconds; a missing end gives None."""
    assert seconds_between(1000, 3500) == 2.5
    assert seconds_between(1000, None) is None


def test_failover_markers_and_times():
    """Every instant traces to a fixture line or a commit height."""
    result = smoke_failover()
    assert result.t_first_failed_check_ms == 1791168879619
    assert result.t_detect_ms == 1791168879620
    assert result.t_takeover_ms == 1791168880683
    assert (result.t_first_signature_ms, result.first_signature_height) == (1791168896910, 144)
    assert (result.last_signed_height, result.gap_heights) == (127, 16)
    assert (result.absent_heights, result.nil_heights) == (15, 1)
    assert result.sources == {
        "fault": "events.jsonl docker kill validator1-node",
        "first_failed_check": "smoke-active.txt:4",
        "detect": "smoke-active.txt:7",
        "takeover": "smoke-passive.txt:12",
        "first_signature": "/commit?height=144",
    }


def test_failover_durations_add_up_to_ttr():
    """TTD 51.620 s, TTR 68.910 s = detection 51.620 + orchestration 1.063 + node start 16.227."""
    result = smoke_failover()
    assert result.ttd_s == pytest.approx(51.620)
    assert result.ttr_s == pytest.approx(68.910)
    assert result.detection_s == pytest.approx(51.620)
    assert result.orchestration_s == pytest.approx(1.063)
    assert result.node_start_s == pytest.approx(16.227)
    assert result.detection_s + result.orchestration_s + result.node_start_s == pytest.approx(result.ttr_s)


def test_failover_without_detection():
    """No detection line: TTD and orchestration stay empty, TTR still comes from the commits."""
    events = [event for event in smoke_events() if event.marker != "max_failures"]
    result = failover_iteration("SFIX", 1, "cfg", FAULT_MS, "x", events, smoke_records(), TARGET_VALIDATOR)
    assert result.ttd_s is None
    assert result.orchestration_s is None
    assert result.ttr_s == pytest.approx(68.910)


def test_failback_from_the_first_healthy_check():
    """Grace 60.001 s, to complete 62.076 s, to signature 76.803 s, execution 2.074 s, 4 heights missed."""
    result = failback_iteration("SFIX", 1, "cfg", FAULT_MS, smoke_events(), smoke_records(), TARGET_VALIDATOR)
    assert result.t_primary_healthy_ms == 1791168899622
    assert result.t_failback_considered_ms == 1791168959623
    assert result.t_failback_initiated_ms == 1791168959624
    assert result.t_failback_complete_ms == 1791168961698
    assert result.t_release_complete_ms == 1791168960768
    assert (result.t_primary_signature_ms, result.primary_signature_height) == (1791168976425, 168)
    assert (result.gap_heights, result.absent_heights, result.nil_heights) == (4, 3, 1)
    assert result.grace_wait_s == pytest.approx(60.001)
    assert result.to_complete_s == pytest.approx(62.076)
    assert result.to_signature_s == pytest.approx(76.803)
    assert result.execution_s == pytest.approx(2.074)
    assert result.sources["primary_healthy"] == "smoke-active.txt:15"
    assert result.sources["release_complete"] == "smoke-passive.txt:20"
    assert result.sources["primary_signature"] == "/commit?height=168"


def test_summaries_refuse_mixed_configurations():
    """IV-3: one SD per configuration, never across configurations."""
    first = asdict(smoke_failover())
    second = asdict(replace(smoke_failover(), iteration=2, ttr_s=70.0))
    assert summarize_field([first, second], "ttr_s").n == 2
    other = {**second, "config_id": "different"}
    with pytest.raises(ValueError, match="mix configurations"):
        summarize_field([first, other], "ttr_s")


def test_summaries_skip_missing_values():
    """An iteration without a value does not enter n."""
    first = asdict(smoke_failover())
    missing = {**first, "iteration": 2, "ttd_s": None}
    assert summarize_field([first, missing], "ttd_s").n == 1


def test_failover_signature_before_process_death_is_ignored():
    """A flag-2 precommit 80 ms after the fault does not end the outage; TTR runs to height 144."""
    result = failover_iteration("SFIX", 1, "cfg", 1791168827679, "x", smoke_events(), smoke_records(),
                                TARGET_VALIDATOR)
    assert result.first_signature_height == 144
    assert result.ttr_s == pytest.approx(69.231)
    assert result.invalid_reason is None


def test_failover_without_a_signing_gap_is_invalid():
    """No unsigned height in the range: TTR and node start are empty and the reason is recorded."""
    records = [record for record in smoke_records() if record.height in (126, 127)]
    result = failover_iteration("SFIX", 1, "cfg", 1791168821000, "x", smoke_events(), records, TARGET_VALIDATOR)
    assert result.ttr_s is None
    assert result.node_start_s is None
    assert result.invalid_reason == "no signing gap observed"


def test_negative_phase_duration_marks_the_row_invalid():
    """A takeover logged after the first signature gives a negative node start, which cannot be real."""
    events = [replace(event, t_ms=1791168990000) if event.marker == "takeover_complete" else event
              for event in smoke_events()]
    result = failover_iteration("SFIX", 1, "cfg", FAULT_MS, "x", events, smoke_records(), TARGET_VALIDATOR)
    assert result.invalid_reason == "negative node_start_s"
    assert result.node_start_s is None
    assert result.ttr_s is None


def test_clock_uncertainty_is_carried_into_the_row():
    """The session's offset uncertainty travels with each TTR row."""
    result = failover_iteration("SFIX", 1, "cfg", FAULT_MS, "x", smoke_events(), smoke_records(),
                                TARGET_VALIDATOR, clock_uncertainty_ms=4.5)
    assert result.clock_uncertainty_ms == 4.5


def make_event(marker: str, t_ms: int, node: str = "validator-1") -> LogEvent:
    """A synthetic log event for pairing tests."""
    return LogEvent(marker, node, t_ms, "info", marker, f"x:{t_ms}", {})


def test_failback_pairs_complete_with_the_last_attempt():
    """considered, initiated, aborted, considered, initiated, complete: the second attempt is paired."""
    events = smoke_events() + [make_event("failback_considered", 1791168990000),
                               make_event("failback_initiated", 1791168990100),
                               make_event("failback_aborted", 1791168990200),
                               make_event("failback_considered", 1791168990300),
                               make_event("failback_initiated", 1791168990400),
                               make_event("failback_complete", 1791168992000)]
    events = [event for event in events if event.t_ms >= 1791168990000 or event.marker not in (
        "failback_considered", "failback_initiated", "failback_complete")]
    result = failback_iteration("SFIX", 1, "cfg", FAULT_MS, events, smoke_records(), TARGET_VALIDATOR)
    assert result.t_failback_considered_ms == 1791168990300
    assert result.t_failback_initiated_ms == 1791168990400
    assert result.t_failback_complete_ms == 1791168992000
    assert result.failback_attempts == 2
