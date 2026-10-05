"""failover_cycle runner: preconditions enforced, events recorded in order."""

import pytest

from fakes import FakeMonitor, FakeProbes, TARGET_ADDRESS, make_session, ready_env, write_key
from harness.logparse import LogEvent
from harness.scenarios.common import PreconditionError
from harness.scenarios.failover import FailoverSettings, run_failover_iteration, run_failover_series
from harness.session import read_events
from harness.signing import HeightRecord

KILL = ("docker", "kill", "validator1-node")
SETTINGS = FailoverSettings()


def event(marker, node, t_ms, source):
    """A LogEvent as LiveProbes would return it."""
    return LogEvent(marker, node, t_ms, "info", marker, source, {})


def cycle_probes(now: int, rounds: int = 1) -> FakeProbes:
    """Probe answers for complete cycles: takeover, backup signature, failback, release, primary signature."""
    markers = {
        ("validator-2", "takeover_complete"): event("takeover_complete", "validator-2", now + 20000, "syncguard-passive.log:12"),
        ("validator-1", "failback_complete"): event("failback_complete", "validator-1", now + 100000, "syncguard.log:22"),
        ("validator-2", "release_complete"): event("release_complete", "validator-2", now + 99000, "syncguard-passive.log:20"),
    }
    signatures = []
    for _ in range(rounds):
        signatures.append(HeightRecord(1200, now + 30000, "P", 0, {TARGET_ADDRESS: now + 30000}, {}, 0))
        signatures.append(HeightRecord(1230, now + 115000, "P", 0, {TARGET_ADDRESS: now + 115000}, {}, 0))
    return FakeProbes(markers, signatures)


def kinds(session):
    """Event kinds in file order."""
    return [row["kind"] for row in read_events(session.directory / "events.jsonl")]


def test_refusal_injects_no_fault(tmp_path):
    """A backup key file holding the validator key stops the run before docker kill."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run_failover_iteration(env, session, 1, SETTINGS, cycle_probes(env.clock.now_ms()))
    assert list(KILL) not in env.commands.calls
    assert kinds(session) == ["iteration_start", "precondition_failed"]


def test_complete_cycle_records_events_in_order(tmp_path):
    """Snapshots at three points, one docker kill, every wait observed, then collection."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "validator1-node\n", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    probes = cycle_probes(env.clock.now_ms())
    outcome = run_failover_iteration(env, session, 1, SETTINGS, probes)
    assert outcome == "complete"
    assert kinds(session) == [
        "iteration_start", "preconditions_passed", "snapshot", "fault_injected", "observed", "observed",
        "snapshot", "observed", "observed", "observed", "snapshot", "stabilization", "heights_collected",
        "iteration_end",
    ]
    assert env.commands.calls.count(list(KILL)) == 1
    fault = next(row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "fault_injected")
    assert (fault["command"], fault["exit_code"], fault["scenario"]) == ("docker kill validator1-node", 0, "failover")
    assert [call[0] for call in probes.calls] == ["marker", "signature", "marker", "marker", "signature"]
    assert (session.directory / "heights.jsonl").exists()
    assert len((session.directory / "snapshots.jsonl").read_text().splitlines()) == 3


def test_missing_takeover_still_collects(tmp_path):
    """A timeout is recorded and the iteration still stores logs and heights."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    outcome = run_failover_iteration(env, session, 1, SETTINGS, FakeProbes({}, []))
    assert outcome == "no_takeover"
    assert kinds(session)[-4:] == ["wait_timeout", "stabilization", "heights_collected", "iteration_end"]
    assert read_events(session.directory / "events.jsonl")[-1]["outcome"] == "no_takeover"


def test_series_runs_the_monitor_around_all_iterations(tmp_path):
    """One monitor start and stop for two iterations; snapshots only in selected iterations."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    outcomes = run_failover_series(env, session, 2, SETTINGS, {1}, cycle_probes(env.clock.now_ms(), rounds=2), monitor)
    assert outcomes == ["complete", "complete"]
    assert (monitor.started, monitor.stopped) == (1, 1)
    assert kinds(session).count("snapshot") == 3


def test_series_stops_the_monitor_on_refusal(tmp_path):
    """The monitor stops even when the first iteration is refused."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(PreconditionError):
        run_failover_series(env, session, 2, SETTINGS, set(), cycle_probes(env.clock.now_ms()), monitor)
    assert (monitor.started, monitor.stopped) == (1, 1)
