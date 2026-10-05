"""Building blocks shared by the runners."""

import pytest

from fakes import FakeClock, FakeCommands, FakeHttp, make_session, paths_under, ready_env
from harness.environment import build_environment
from harness.logparse import LogEvent
from harness.scenarios.common import (
    PreconditionError,
    container_status,
    docker_action,
    log_observation,
    next_iteration,
    record_window,
    require,
)
from harness.session import read_events
from harness.signing import HeightRecord

NOW = 1791169200000


def test_require_logs_and_raises(tmp_path):
    """Failures are logged with the scenario and raised; a pass is logged too."""
    session = make_session(tmp_path, FakeClock(NOW))
    require(session, "failover", [], 1)
    with pytest.raises(PreconditionError) as raised:
        require(session, "failover", ["chain not advancing"], 2)
    assert raised.value.failures == ["chain not advancing"]
    kinds = [(event["kind"], event["iteration"]) for event in read_events(session.directory / "events.jsonl")]
    assert kinds == [("preconditions_passed", 1), ("precondition_failed", 2)]


def test_next_iteration_counts_started_iterations(tmp_path):
    """Numbering continues across runs in one session."""
    session = make_session(tmp_path, FakeClock(NOW))
    assert next_iteration(session) == 1
    session.events.append("iteration_start", iteration=1)
    session.events.append("iteration_start", iteration=4)
    assert next_iteration(session) == 5


def test_docker_action_logs_the_exact_command(tmp_path):
    """The event carries the command, exit code, and the issue instant as t_ms."""
    clock = FakeClock(NOW)
    commands = FakeCommands({("docker", "kill", "validator1-node"): [(0, "validator1-node\n", "")]}, clock)
    env = build_environment(FakeHttp(), commands, clock, paths_under(tmp_path), 25200)
    session = make_session(env.paths.data_dir, clock)
    docker_action(env, session, "kill", "validator1-node", "failover", 3, "fault_injected", height=1192)
    event = read_events(session.directory / "events.jsonl")[-1]
    assert (event["kind"], event["t_ms"], event["command"], event["exit_code"], event["height"]) == (
        "fault_injected", NOW, "docker kill validator1-node", 0, 1192)


def test_log_observation_variants(tmp_path):
    """Timeouts, log events, and signed heights each leave one event."""
    session = make_session(tmp_path, FakeClock(NOW))
    log_observation(session, 1, "takeover_complete", None)
    log_observation(session, 1, "takeover_complete", LogEvent("takeover_complete", "validator-2", NOW, "info", "m", "x.log:9"))
    log_observation(session, 1, "backup_first_signature", HeightRecord(144, NOW, "P", 0, {}, {}, 0))
    rows = read_events(session.directory / "events.jsonl")
    assert [row["kind"] for row in rows] == ["wait_timeout", "observed", "observed"]
    assert rows[1]["source"] == "x.log:9"
    assert rows[2]["height"] == 144


def test_record_window_tags_events_with_the_iteration(tmp_path):
    """Six primary and five backup events fall in the failback window; they are appended in time order."""
    env = ready_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    events = record_window(env, session, 7, 1791168959623, 1791168961698)
    rows = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "syncguard"]
    assert len(rows) == len(events) == 11
    assert {row["iteration"] for row in rows} == {7}
    assert rows[0]["marker"] == "failback_considered"
    assert [row["t_ms"] for row in rows] == sorted(row["t_ms"] for row in rows)


def test_container_status(tmp_path):
    """docker inspect output, or unknown on failure."""
    clock = FakeClock(NOW)
    commands = FakeCommands({
        ("docker", "inspect", "-f", "{{.State.Status}}", "validator1-node"): [(0, "paused\n", "")],
        ("docker", "inspect", "-f", "{{.State.Status}}", "missing"): [(1, "", "No such object")],
    }, clock)
    env = build_environment(FakeHttp(), commands, clock, paths_under(tmp_path), 25200)
    assert container_status(env, "validator1-node") == "paused"
    assert container_status(env, "missing") == "unknown"
