"""failover_cycle runner: preconditions enforced, events recorded in order."""

from dataclasses import replace

import pytest

from fakes import FakeMonitor, FakeProbes, TARGET_ADDRESS, make_session, ready_env, write_key
from harness.adapters import Unreachable
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
        "snapshot", "observed", "observed", "observed", "snapshot", "stabilization", "iteration_result",
        "heights_collected", "iteration_end",
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
    assert kinds(session)[-5:] == ["wait_timeout", "stabilization", "iteration_result", "heights_collected", "iteration_end"]
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


def test_series_stops_after_an_outcome_other_than_complete(tmp_path):
    """A takeover timeout aborts the series with a series_aborted event; the monitor still stops."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    outcomes = run_failover_series(env, session, 3, SETTINGS, set(), FakeProbes({}, []), monitor)
    assert outcomes == ["no_takeover"]
    assert env.commands.calls.count(list(KILL)) == 1
    aborted = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "series_aborted"]
    assert [(row["iteration"], row["outcome"]) for row in aborted] == [(1, "no_takeover")]
    assert (monitor.started, monitor.stopped) == (1, 1)


def test_series_keeps_completed_iterations_before_the_abort(tmp_path):
    """Iteration 1 completes, iteration 2 finds no scripted signature: both outcomes returned, series_aborted names iteration 2."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    outcomes = run_failover_series(env, session, 3, SETTINGS, set(), cycle_probes(env.clock.now_ms()), FakeMonitor())
    assert outcomes == ["complete", "missing_signature"]
    aborted = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "series_aborted"]
    assert [(row["iteration"], row["outcome"]) for row in aborted] == [(2, "missing_signature")]


class ExplodingProbes(FakeProbes):
    """Probes whose marker wait raises the given exception."""

    def __init__(self, error: BaseException):
        """Keep the exception to raise."""
        super().__init__({}, [])
        self.error = error

    def marker(self, node, marker, since_ms, timeout_s):
        """Raise the configured exception."""
        raise self.error


def test_exception_after_the_kill_is_logged_and_reraised(tmp_path):
    """The abort event carries the error text, the monitor stops, and the exception propagates."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(OSError):
        run_failover_series(env, session, 1, SETTINGS, set(), ExplodingProbes(OSError("log read failed")), monitor)
    aborted = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "iteration_aborted"]
    assert len(aborted) == 1 and "log read failed" in aborted[0]["error"]
    assert (monitor.started, monitor.stopped) == (1, 1)


def test_interrupt_after_the_kill_is_logged_as_interrupted(tmp_path):
    """Ctrl-C after the kill logs iteration_interrupted, stops the monitor, and propagates."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(KeyboardInterrupt):
        run_failover_series(env, session, 1, SETTINGS, set(), ExplodingProbes(KeyboardInterrupt()), monitor)
    kinds_seen = kinds(session)
    assert "iteration_interrupted" in kinds_seen and "iteration_aborted" not in kinds_seen
    assert (monitor.started, monitor.stopped) == (1, 1)


INSPECT = ("docker", "inspect", "-f", "{{.State.Status}}", "validator1-node")


def test_failed_kill_command_proceeds_only_if_the_container_is_stopped(tmp_path):
    """Exit -1 with an exited container is followed through; with a running one the iteration aborts."""
    env = ready_env(tmp_path, extra_script={KILL: [(-1, "", "timeout")], INSPECT: [(0, "exited\n", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    assert run_failover_iteration(env, session, 1, SETTINGS, cycle_probes(env.clock.now_ms())) == "complete"
    running = ready_env(tmp_path / "b", extra_script={KILL: [(-1, "", "timeout")], INSPECT: [(0, "running\n", "")]})
    other = make_session(running.paths.data_dir, running.clock)
    assert run_failover_iteration(running, other, 1, SETTINGS, cycle_probes(running.clock.now_ms())) == "fault_command_failed"


def test_signatures_are_searched_from_the_end_of_the_kill_command(tmp_path):
    """A precommit cast before the kill command finished cannot count: the bound is its ended_ms."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    original = env.commands.run

    def slow_kill(argv, timeout=120.0):
        """Run the scripted command; the kill takes 2 s."""
        result = original(argv, timeout)
        return replace(result, ended_ms=result.ended_ms + 2000) if argv[1] == "kill" else result

    env.commands.run = slow_kill
    session = make_session(env.paths.data_dir, env.clock)
    probes = cycle_probes(env.clock.now_ms())
    run_failover_iteration(env, session, 1, SETTINGS, probes)
    kill_event = next(row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "fault_injected")
    assert probes.calls[1] == ("signature", kill_event["ended_ms"])


def test_heights_error_keeps_the_iteration_result_and_logs_an_abort(tmp_path):
    """The outcome is logged before final heights are collected; a collection error logs iteration_aborted and re-raises."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    working = env.http.answers["http://localhost:26657/commit?height=*"]

    def commits_fail_after_the_kill(url):
        """Normal commits until the kill is logged, then the rpc is down."""
        killed = any(row["kind"] == "fault_injected" for row in read_events(session.directory / "events.jsonl"))
        return Unreachable("rpc down") if killed else working(url)

    env.http.answers["http://localhost:26657/commit?height=*"] = commits_fail_after_the_kill
    with pytest.raises(Unreachable):
        run_failover_iteration(env, session, 1, SETTINGS, cycle_probes(env.clock.now_ms()))
    rows = read_events(session.directory / "events.jsonl")
    results = [row for row in rows if row["kind"] == "iteration_result"]
    assert [row["outcome"] for row in results] == ["complete"]
    assert any(row["kind"] == "iteration_aborted" for row in rows)
