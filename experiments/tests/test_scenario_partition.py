"""partition runner: docker network disconnect of the primary, detection, takeover, reconnect, failback."""

import pytest

from fakes import FakeMonitor, FakeProbes, TARGET_ADDRESS, make_session, ready_env, write_key
from harness.adapters import Unreachable
from harness.jsonio import read_json
from harness.logparse import LogEvent
from harness.scenarios.common import PreconditionError, container_networks
from harness.scenarios.partition import (
    RESULT_KEYS,
    PartitionSettings,
    ReconnectError,
    run_partition,
    run_partition_series,
)
from harness.session import read_events
from harness.signing import HeightRecord

NETWORK = "story-localnet_default"
INSPECT_NETWORKS = ("docker", "inspect", "validator1-node", "--format", "{{json .NetworkSettings.Networks}}")
DISCONNECT = ("docker", "network", "disconnect", NETWORK, "validator1-node")
CONNECT = ("docker", "network", "connect", NETWORK, "validator1-node")
ATTACHED = (0, '{"story-localnet_default": {"NetworkID": "abc"}}\n', "")
DETACHED = (0, "{}\n", "")
SETTINGS = PartitionSettings()


def event(marker, node, t_ms, source):
    """A LogEvent as LiveProbes would return it."""
    return LogEvent(marker, node, t_ms, "info", marker, source, {})


def cycle_probes(fault_ms: int, rounds: int = 1) -> FakeProbes:
    """Probe answers for complete cycles: detection, takeover, backup signature, failback, release, primary signature."""
    markers = {
        ("validator-1", "max_failures"): event("max_failures", "validator-1", fault_ms + 33000, "syncguard.log:80"),
        ("validator-2", "takeover_complete"): event("takeover_complete", "validator-2", fault_ms + 50000, "passive:12"),
        ("validator-1", "failback_complete"): event("failback_complete", "validator-1", fault_ms + 200000, "syncguard.log:90"),
        ("validator-2", "release_complete"): event("release_complete", "validator-2", fault_ms + 199000, "passive:20"),
    }
    signatures = []
    for _ in range(rounds):
        signatures.append(HeightRecord(1200, fault_ms + 60000, "P", 0, {TARGET_ADDRESS: fault_ms + 60000}, {}, 0))
        signatures.append(HeightRecord(1260, fault_ms + 215000, "P", 0, {TARGET_ADDRESS: fault_ms + 215000}, {}, 0))
    return FakeProbes(markers, signatures)


def partition_env(tmp_path, inspect_answers=None, script=None):
    """A ready environment whose primary sits on one network and whose disconnect and connect succeed."""
    commands = {INSPECT_NETWORKS: inspect_answers or [ATTACHED], DISCONNECT: [(0, "", "")], CONNECT: [(0, "", "")], **(script or {})}
    return ready_env(tmp_path, extra_script=commands)


def kinds(session):
    """Event kinds in file order."""
    return [row["kind"] for row in read_events(session.directory / "events.jsonl")]


def fault_time(env) -> int:
    """The disconnect instant: preconditions sleep 6 s while checking chain progress."""
    return env.clock.now_ms() + 6000


def test_container_networks_reads_the_names_from_docker_inspect(tmp_path):
    """The names come from docker inspect, never from a constant; a failed or malformed answer is None."""
    env = partition_env(tmp_path)
    assert container_networks(env, "validator1-node") == [NETWORK]
    broken = partition_env(tmp_path, inspect_answers=[(1, "", "no such container")])
    assert container_networks(broken, "validator1-node") is None
    malformed = partition_env(tmp_path, inspect_answers=[(0, "[1, 2]", "")])
    assert container_networks(malformed, "validator1-node") is None


def test_refusal_disconnects_nothing(tmp_path):
    """Preconditions run before any network command; a backup holding the validator key stops the run."""
    env = partition_env(tmp_path)
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run_partition(env, session, 1, SETTINGS, FakeProbes({}, []), FakeMonitor())
    assert list(DISCONNECT) not in env.commands.calls
    assert list(INSPECT_NETWORKS) not in env.commands.calls
    assert kinds(session) == ["iteration_start", "precondition_failed"]


def test_a_primary_on_two_networks_is_refused(tmp_path):
    """A partition needs exactly one network; with two the disconnect would not isolate the node."""
    two = (0, '{"a": {}, "b": {}}', "")
    env = partition_env(tmp_path, inspect_answers=[two])
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError) as refused:
        run_partition(env, session, 1, SETTINGS, FakeProbes({}, []), FakeMonitor())
    assert "2 networks" in refused.value.failures[0]
    assert list(DISCONNECT) not in env.commands.calls


def test_complete_run_records_events_in_order_and_the_result(tmp_path):
    """Disconnect, detection, takeover, backup signature, reconnect, failback, primary signature, settle, collection."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    fault_ms = fault_time(env)
    monitor = FakeMonitor()
    result = run_partition(env, session, 1, SETTINGS, cycle_probes(fault_ms), monitor)
    assert kinds(session) == [
        "iteration_start", "preconditions_passed", "fault_injected", "observed", "observed", "observed",
        "fault_cleared", "observed", "observed", "observed", "post_recovery_monitoring", "iteration_result",
        "heights_collected", "iteration_end",
    ]
    rows = read_events(session.directory / "events.jsonl")
    fault = next(row for row in rows if row["kind"] == "fault_injected")
    assert (fault["fault_kind"], fault["scenario"], fault["exit_code"], fault["network"]) == ("partition", "partition", 0, NETWORK)
    assert fault["command"] == f"docker network disconnect {NETWORK} validator1-node"
    assert (fault["t_ms"], fault["ended_ms"]) == (fault_ms, fault_ms)
    assert fault["height"] == result["fault_height"] >= 1188
    cleared = next(row for row in rows if row["kind"] == "fault_cleared")
    assert cleared["command"] == f"docker network connect {NETWORK} validator1-node"
    assert env.commands.calls.index(list(DISCONNECT)) < env.commands.calls.index(list(CONNECT))
    assert (result["outcome"], result["network"], result["t_fault_ms"], result["fault_height"]) == ("complete", NETWORK, fault_ms, fault["height"])
    assert (result["detected"], result["ttd_s"], result["takeover"]) == (True, 33.0, True)
    assert (result["backup_first_signature_height"], result["ttr_s"]) == (1200, 60.0)
    assert (result["failback_complete"], result["primary_first_signature_height"]) == (True, 1260)
    assert result["reconnect_ms"] == cleared["t_ms"] and result["reconnect_ms"] >= fault_ms
    assert set(result["monitor"]) == {"observed", "observed_problems", "dual_signer_seconds", "indeterminate_seconds"}
    assert result["monitor"]["dual_signer_seconds"] is None and result["monitor"]["observed"] is False
    assert set(RESULT_KEYS) <= set(result)
    assert 60.0 in env.clock.slept
    assert (monitor.started, monitor.stopped) == (1, 1)
    assert read_json(session.directory / "partition-1.json")["outcome"] == "complete"


def test_signature_search_starts_at_the_disconnect_end_and_runs_once_per_side(tmp_path):
    """The backup's signature is searched from the end of the disconnect command; the primary's after the release."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    fault_ms = fault_time(env)
    probes = cycle_probes(fault_ms)
    run_partition(env, session, 1, SETTINGS, probes, FakeMonitor())
    assert [call[0] for call in probes.calls] == ["marker", "marker", "signature", "marker", "marker", "signature"]
    assert probes.calls[2] == ("signature", fault_ms)
    assert probes.calls[5] == ("signature", fault_ms + 199000)


def test_undetected_partition_is_reconnected_and_recorded(tmp_path):
    """No detection inside the bound: no takeover wait, the container is reconnected, the outcome is not_detected."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    result = run_partition(env, session, 1, SETTINGS, FakeProbes({}, []), FakeMonitor())
    assert (result["outcome"], result["detected"], result["ttd_s"], result["takeover"]) == ("not_detected", False, None, False)
    assert (result["failback_complete"], result["ttr_s"]) == (False, None)
    assert env.commands.calls.count(list(CONNECT)) == 1
    assert kinds(session)[-6:] == ["wait_timeout", "fault_cleared", "post_recovery_monitoring", "iteration_result",
                                  "heights_collected", "iteration_end"]


def test_failed_disconnect_on_a_connected_container_ends_without_reconnect(tmp_path):
    """The disconnect failed and the container is still attached: fault_command_failed, nothing to undo."""
    env = partition_env(tmp_path, script={DISCONNECT: [(1, "", "boom")]})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_partition(env, session, 1, SETTINGS, FakeProbes({}, []), FakeMonitor())
    assert (result["outcome"], result["fault_exit_code"], result["detected"]) == ("fault_command_failed", 1, False)
    assert list(CONNECT) not in env.commands.calls
    assert "fault_command_failed" in kinds(session)


def test_failed_reconnect_aborts_and_the_cleanup_retries_twice(tmp_path):
    """A connect that fails in the normal path aborts the run; the cleanup retries it twice and logs cleanup_failed."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, DETACHED], script={CONNECT: [(1, "", "no")]})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(ReconnectError):
        run_partition(env, session, 1, SETTINGS, cycle_probes(fault_time(env)), FakeMonitor())
    assert env.commands.calls.count(list(CONNECT)) == 3
    failed = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "cleanup_failed"]
    assert [row["step"] for row in failed] == ["reconnect"]
    assert not (session.directory / "partition-1.json").exists()


def test_result_is_written_before_the_final_heights(tmp_path, monkeypatch):
    """A heights failure keeps the result JSON."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    seen = []

    def failing_collection(env_, session_, first, last):
        """Record whether the result exists, then fail like an unreachable RPC."""
        seen.append((session_.directory / "partition-1.json").exists())
        raise Unreachable("rpc down")

    monkeypatch.setattr("harness.scenarios.partition.collect_heights_in_chunks", failing_collection)
    with pytest.raises(Unreachable):
        run_partition(env, session, 1, SETTINGS, cycle_probes(fault_time(env)), FakeMonitor())
    assert seen == [True]
    assert "iteration_end" not in kinds(session)
    assert read_json(session.directory / "partition-1.json")["outcome"] == "complete"


class FailingProbes(FakeProbes):
    """Probes whose backup signature wait raises, standing for an abort in the middle of the run."""

    def __init__(self, error: BaseException):
        """Detect at once, then raise the given error from the signature wait."""
        super().__init__({("validator-1", "max_failures"): event("max_failures", "validator-1", 0, "s:1"),
                          ("validator-2", "takeover_complete"): event("takeover_complete", "validator-2", 0, "p:1")}, [])
        self.error = error

    def signature(self, after_ms, start_height, timeout_s):
        """Raise the configured error."""
        raise self.error


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt()])
def test_cleanup_reconnects_after_an_exception_or_interrupt_and_reraises(tmp_path, error):
    """An abort while disconnected reconnects the container (it is absent from the networks) and re-raises."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, DETACHED])
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(type(error)):
        run_partition(env, session, 1, SETTINGS, FailingProbes(error), monitor)
    assert env.commands.calls.count(list(CONNECT)) == 1
    names = kinds(session)
    assert ("iteration_interrupted" if isinstance(error, KeyboardInterrupt) else "iteration_aborted") in names
    reconnect = next(row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "abort_reconnect")
    assert reconnect["exit_code"] == 0
    assert monitor.stopped == 1
    assert not (session.directory / "partition-1.json").exists()


def test_cleanup_does_nothing_when_the_container_was_never_disconnected(tmp_path, monkeypatch):
    """An abort before the disconnect issues no network command and logs the skip."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    real_tip = env.rpc.tip_height

    def tip_fails_after_inspect() -> int:
        """Read the tip normally during preconditions; fail once the network inspect has run."""
        if list(INSPECT_NETWORKS) in env.commands.calls:
            raise RuntimeError("rpc down")
        return real_tip()

    monkeypatch.setattr(env.rpc, "tip_height", tip_fails_after_inspect)
    with pytest.raises(RuntimeError):
        run_partition(env, session, 1, SETTINGS, FakeProbes({}, []), FakeMonitor())
    assert list(DISCONNECT) not in env.commands.calls and list(CONNECT) not in env.commands.calls
    assert "abort_cleanup_skipped" in kinds(session)


def test_cleanup_skips_the_connect_when_the_container_is_attached_again(tmp_path):
    """After an abort the inspect shows the network; no second connect is issued."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, ATTACHED])
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(RuntimeError):
        run_partition(env, session, 1, SETTINGS, FailingProbes(RuntimeError("boom")), FakeMonitor())
    assert list(CONNECT) not in env.commands.calls
    assert "abort_cleanup_skipped" in kinds(session)


def test_cleanup_reconnect_failure_is_logged_and_the_original_error_wins(tmp_path):
    """Two failed connects log cleanup_failed with the step; the abort's own exception still propagates."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, DETACHED], script={CONNECT: [(1, "", "no")]})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(RuntimeError, match="boom"):
        run_partition(env, session, 1, SETTINGS, FailingProbes(RuntimeError("boom")), FakeMonitor())
    assert env.commands.calls.count(list(CONNECT)) == 2
    failed = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "cleanup_failed"]
    assert [row["step"] for row in failed] == ["reconnect"]


def test_series_stops_on_the_first_outcome_other_than_complete(tmp_path):
    """The second iteration is never started after a not_detected outcome; series_aborted names it."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    results = run_partition_series(env, session, 3, SETTINGS, FakeProbes({}, []), monitor)
    assert [row["outcome"] for row in results] == ["not_detected"]
    assert env.commands.calls.count(list(DISCONNECT)) == 1
    aborted = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "series_aborted"]
    assert [(row["iteration"], row["outcome"], row["scenario"]) for row in aborted] == [(1, "not_detected", "partition")]


def test_series_runs_every_iteration_when_each_completes(tmp_path):
    """Two complete iterations are numbered after the session's earlier ones and each writes its own JSON."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    session.events.append("iteration_start", scenario="hang", iteration=4)
    fault_ms = fault_time(env)
    probes = cycle_probes(fault_ms, rounds=2)
    results = run_partition_series(env, session, 2, SETTINGS, probes, FakeMonitor())
    assert [row["outcome"] for row in results] == ["complete", "complete"]
    assert [row["iteration"] for row in results] == [5, 6]
    assert (session.directory / "partition-5.json").exists() and (session.directory / "partition-6.json").exists()
