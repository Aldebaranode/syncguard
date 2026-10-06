"""partition runner: docker network disconnect of the primary, detection, takeover, reconnect, failback."""

import pytest

from fakes import FakeMonitor, FakeProbes, TARGET_ADDRESS, make_session, ready_env, write_key
from harness.adapters import Unreachable
from harness.jsonio import read_json
from harness.logparse import LogEvent
from harness.scenarios.common import PreconditionError, container_endpoints, container_networks
from harness.scenarios.partition import (
    RESULT_KEYS,
    PartitionSettings,
    ReconnectError,
    classify_symptom,
    run_partition,
    run_partition_series,
)
from harness.session import read_events
from harness.signing import HeightRecord

NETWORK = "story-localnet_default"
IP = "10.0.0.21"
INSPECT_NETWORKS = ("docker", "inspect", "validator1-node", "--format", "{{json .NetworkSettings.Networks}}")
INSPECT_STATE = ("docker", "inspect", "-f", "{{.State.Status}}", "validator1-node")
DISCONNECT = ("docker", "network", "disconnect", NETWORK, "validator1-node")
CONNECT = ("docker", "network", "connect", "--ip", IP, "--alias", "validator1-node", "--alias", "abc123", NETWORK,
           "validator1-node")
ENDPOINT = ('{"story-localnet_default": {"IPAMConfig": {"IPv4Address": "10.0.0.21"}, "IPAddress": "10.0.0.21", '
            '"Aliases": ["validator1-node", "abc123"]}}')
ATTACHED = (0, ENDPOINT + "\n", "")
DETACHED = (0, "{}\n", "")
WRONG_IP = (0, ENDPOINT.replace('"IPAddress": "10.0.0.21"', '"IPAddress": "10.0.0.99"'), "")
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
        ("validator-1", "container_restarted"): event("container_restarted", "validator-1", fault_ms + 40000, "syncguard.log:85"),
    }
    signatures = []
    for _ in range(rounds):
        signatures.append(HeightRecord(1200, fault_ms + 60000, "P", 0, {TARGET_ADDRESS: fault_ms + 60000}, {}, 0))
        signatures.append(HeightRecord(1260, fault_ms + 215000, "P", 0, {TARGET_ADDRESS: fault_ms + 215000}, {}, 0))
    return FakeProbes(markers, signatures)


def partition_env(tmp_path, inspect_answers=None, script=None, durations=None):
    """A ready environment whose primary sits on one network at the compose address; connect and disconnect succeed.

    The network inspect answers in order: preconditions, before the reconnect, after it, then the last repeats.
    """
    commands = {INSPECT_NETWORKS: inspect_answers or [ATTACHED, DETACHED, ATTACHED],
                INSPECT_STATE: [(0, "running\n", "")], DISCONNECT: [(0, "", "")], CONNECT: [(0, "", "")],
                **(script or {})}
    return ready_env(tmp_path, extra_script=commands, durations=durations)


def kinds(session):
    """Event kinds in file order."""
    return [row["kind"] for row in read_events(session.directory / "events.jsonl")]


def rows_of(session, kind):
    """Event rows of one kind."""
    return [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == kind]


def fault_time(env) -> int:
    """The disconnect instant: preconditions sleep 6 s while checking chain progress."""
    return env.clock.now_ms() + 6000


def run(env, session, probes, monitor=None, iteration=1, said=None):
    """run_partition with the test's recording say function."""
    return run_partition(env, session, iteration, SETTINGS, probes, monitor or FakeMonitor(),
                         say=(said if said is not None else []).append)


def test_container_endpoints_keep_the_static_address_and_aliases(tmp_path):
    """The names, the static address, the current address, and the aliases come from docker inspect only."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED])
    assert container_endpoints(env, "validator1-node") == {
        NETWORK: {"ip": IP, "address": IP, "aliases": ["validator1-node", "abc123"]}}
    assert container_networks(env, "validator1-node") == [NETWORK]
    assert container_networks(partition_env(tmp_path, inspect_answers=[(1, "", "gone")]), "validator1-node") is None
    assert container_networks(partition_env(tmp_path, inspect_answers=[(0, "[1, 2]", "")]), "validator1-node") is None


def test_refusal_disconnects_nothing(tmp_path):
    """Preconditions run before any network change; a backup holding the validator key stops the run."""
    env = partition_env(tmp_path)
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run(env, session, FakeProbes({}, []))
    assert list(DISCONNECT) not in env.commands.calls
    assert kinds(session) == ["precondition_failed"]


def test_refusal_lists_every_failure_together(tmp_path):
    """A failover precondition and the network preconditions are reported in one refusal."""
    two = (0, '{"a": {}, "b": {}}', "")
    env = partition_env(tmp_path, inspect_answers=[two])
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError) as refused:
        run(env, session, FakeProbes({}, []))
    text = " | ".join(refused.value.failures)
    assert "backup key file holds the validator key" in text and "2 networks" in text


def test_a_primary_away_from_the_compose_address_is_refused(tmp_path):
    """The reconnect restores the compose address, so a primary that is not on it refuses the run."""
    env = partition_env(tmp_path, inspect_answers=[WRONG_IP])
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError) as refused:
        run(env, session, FakeProbes({}, []))
    assert "10.0.0.99" in refused.value.failures[0] and IP in refused.value.failures[0]
    assert list(DISCONNECT) not in env.commands.calls


def test_complete_run_records_events_in_order_and_the_result(tmp_path):
    """Disconnect, detection, takeover, backup signature, reconnect with the original endpoint, failback, settle."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    fault_ms = fault_time(env)
    result = run(env, session, cycle_probes(fault_ms))
    assert kinds(session) == [
        "preconditions_passed", "iteration_start", "fault_injected", "observed", "observed", "observed",
        "fault_cleared", "observed", "observed", "observed", "post_recovery_monitoring", "iteration_result",
        "heights_collected", "iteration_end",
    ]
    fault = rows_of(session, "fault_injected")[0]
    assert (fault["fault_kind"], fault["scenario"], fault["exit_code"], fault["network"]) == ("partition", "partition", 0, NETWORK)
    assert fault["command"] == f"docker network disconnect {NETWORK} validator1-node"
    assert (fault["t_ms"], fault["ended_ms"]) == (fault_ms, fault_ms)
    assert fault["height"] == result["fault_height"] >= 1188
    cleared = rows_of(session, "fault_cleared")[0]
    assert cleared["command"] == " ".join(CONNECT)
    assert env.commands.calls.index(list(DISCONNECT)) < env.commands.calls.index(list(CONNECT))
    assert (result["outcome"], result["network"], result["t_fault_ms"]) == ("complete", NETWORK, fault_ms)
    assert (result["detected"], result["ttd_s"], result["takeover"]) == (True, 33.0, True)
    assert (result["backup_first_signature_height"], result["ttr_s"]) == (1200, 60.0)
    assert (result["failback_complete"], result["primary_first_signature_height"]) == (True, 1260)
    assert result["reconnect_ms"] == cleared["t_ms"] >= fault_ms
    assert (result["restart_outcome"], result["restart_source"]) == ("succeeded", "syncguard.log:85")
    assert (result["container_state_before_reconnect"], result["attached_before_reconnect"]) == ("running", False)
    assert (result["final_container_state"], result["final_attached"], result["final_ip"]) == ("running", True, IP)
    assert set(RESULT_KEYS) <= set(result)
    assert set(result["monitor"]) == {"observed", "observed_problems", "dual_signer_seconds", "indeterminate_seconds"}
    assert 100.0 in env.clock.slept
    assert read_json(session.directory / "partition-1.json")["outcome"] == "complete"


def test_the_run_does_not_own_the_monitor(tmp_path):
    """The series starts and stops the monitor; one iteration only reads its samples."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    run(env, session, cycle_probes(fault_time(env)), monitor)
    assert (monitor.started, monitor.stopped) == (0, 0)


def test_signature_search_starts_at_the_disconnect_end(tmp_path):
    """The backup's signature is searched from the end of a slow disconnect, not its start."""
    env = partition_env(tmp_path, durations={DISCONNECT: 4000})
    session = make_session(env.paths.data_dir, env.clock)
    fault_ms = fault_time(env)
    probes = cycle_probes(fault_ms)
    run(env, session, probes)
    assert rows_of(session, "fault_injected")[0]["ended_ms"] == fault_ms + 4000
    assert [call for call in probes.calls if call[0] == "signature"] == [
        ("signature", fault_ms + 4000), ("signature", fault_ms + 199000)]


class WaitRecordingProbes(FakeProbes):
    """Probes that record every wait's timeout and move the fake clock 50 s per marker wait."""

    def __init__(self, clock, markers):
        """Keep the clock; no signatures are scripted."""
        super().__init__(markers, [])
        self.clock = clock
        self.waits = {}

    def marker(self, node, marker, since_ms, timeout_s):
        """Record the first timeout per marker, spend 50 s, then answer like FakeProbes."""
        self.waits.setdefault(marker, timeout_s)
        self.clock.sleep(50)
        return super().marker(node, marker, since_ms, timeout_s)

    def signature(self, after_ms, start_height, timeout_s):
        """Record the first signature timeout, then answer like FakeProbes."""
        self.waits.setdefault("signature", timeout_s)
        return super().signature(after_ms, start_height, timeout_s)


def test_one_deadline_bounds_detection_takeover_and_the_backup_signature(tmp_path):
    """Every wait before the reconnect shares the 180 s deadline counted from the end of the disconnect command."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    probes = WaitRecordingProbes(env.clock, cycle_probes(fault_time(env)).markers)
    run(env, session, probes)
    assert probes.waits["max_failures"] == 60.0
    assert probes.waits["takeover_complete"] == pytest.approx(130.0)
    assert probes.waits["signature"] == pytest.approx(80.0)


def test_undetected_partition_is_reconnected_and_recorded(tmp_path):
    """No detection inside the bound: no takeover wait, the container is reconnected, the outcome is not_detected."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    result = run(env, session, FakeProbes({}, []))
    assert (result["outcome"], result["detected"], result["ttd_s"], result["takeover"]) == ("not_detected", False, None, False)
    assert (result["failback_complete"], result["ttr_s"], result["detection_symptom"]) == (False, None, None)
    assert env.commands.calls.count(list(CONNECT)) == 1
    assert kinds(session)[-6:] == ["wait_timeout", "fault_cleared", "post_recovery_monitoring", "iteration_result",
                                  "heights_collected", "iteration_end"]


def test_a_restart_that_left_the_container_attached_needs_no_connect(tmp_path):
    """When the inspect before the reconnect already shows the network, no connect is issued and that is recorded."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED])
    session = make_session(env.paths.data_dir, env.clock)
    result = run(env, session, cycle_probes(fault_time(env)))
    assert list(CONNECT) not in env.commands.calls
    assert (result["attached_before_reconnect"], result["reconnect_exit_code"], result["outcome"]) == (True, None, "complete")


def test_a_reconnect_to_another_address_is_reconnect_failed_and_stored(tmp_path):
    """Verification after the connect compares the address; a mismatch aborts, stores the result, and says so."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, DETACHED, WRONG_IP, WRONG_IP, DETACHED, WRONG_IP])
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    with pytest.raises(ReconnectError):
        run(env, session, cycle_probes(fault_time(env)), said=said)
    stored = read_json(session.directory / "partition-1.json")
    assert stored["outcome"] == "reconnect_failed" and "10.0.0.99" in stored["error"]
    assert stored["detected"] is True and stored["backup_first_signature_height"] == 1200


def test_failed_disconnect_on_a_connected_container_ends_without_reconnect(tmp_path):
    """The disconnect failed and the container is still attached: fault_command_failed, nothing to undo."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED], script={DISCONNECT: [(1, "", "boom")]})
    session = make_session(env.paths.data_dir, env.clock)
    result = run(env, session, FakeProbes({}, []))
    assert (result["outcome"], result["fault_exit_code"], result["detected"]) == ("fault_command_failed", 1, False)
    assert list(CONNECT) not in env.commands.calls
    failed = rows_of(session, "fault_command_failed")[0]
    assert failed["container_state"] == "running" and failed["container_networks"] == [NETWORK]


def test_failed_disconnect_with_an_unreadable_inspect_aborts_and_reconnects(tmp_path):
    """Without proof the container is still attached the run does not follow; it aborts through the cleanup."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, (1, "", "gone"), (1, "", "gone"), DETACHED],
                        script={DISCONNECT: [(1, "", "boom")]})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(RuntimeError, match="cannot confirm"):
        run(env, session, cycle_probes(fault_time(env)))
    assert "iteration_aborted" in kinds(session)
    assert read_json(session.directory / "partition-1.json")["outcome"] == "aborted"


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
        run(env, session, cycle_probes(fault_time(env)))
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


@pytest.mark.parametrize("error, outcome", [(RuntimeError("boom"), "aborted"), (KeyboardInterrupt(), "interrupted")])
def test_abort_reconnects_stores_the_partial_result_and_reraises(tmp_path, error, outcome):
    """An abort while disconnected reconnects with the original endpoint, writes what was measured, and re-raises."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, DETACHED, ATTACHED])
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    with pytest.raises(type(error)):
        run(env, session, FailingProbes(error), said=said)
    assert env.commands.calls.count(list(CONNECT)) == 1
    assert ("iteration_interrupted" if isinstance(error, KeyboardInterrupt) else "iteration_aborted") in kinds(session)
    reconnect = rows_of(session, "abort_reconnect")[0]
    assert reconnect["exit_code"] == 0 and reconnect["command"] == " ".join(CONNECT)
    stored = read_json(session.directory / "partition-1.json")
    assert (stored["outcome"], stored["detected"], stored["takeover"]) == (outcome, True, True)
    assert stored["final_attached"] is True and set(RESULT_KEYS) <= set(stored)
    assert said == []


def test_abort_that_cannot_reconnect_prints_the_exact_command(tmp_path):
    """Still disconnected after the cleanup: cleanup_failed is logged and the operator is told the command with --ip."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, DETACHED], script={CONNECT: [(1, "", "no")]})
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    with pytest.raises(RuntimeError, match="boom"):
        run(env, session, FailingProbes(RuntimeError("boom")), said=said)
    assert env.commands.calls.count(list(CONNECT)) == 2
    assert [row["step"] for row in rows_of(session, "cleanup_failed")] == ["reconnect"]
    assert len(said) == 1 and " ".join(CONNECT) in said[0]
    assert read_json(session.directory / "partition-1.json")["final_attached"] is False


def test_failed_reconnect_in_the_normal_path_aborts_and_the_cleanup_retries(tmp_path):
    """A connect that fails after the wait aborts; the cleanup retries twice and the result says reconnect_failed."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, DETACHED], script={CONNECT: [(1, "", "no")]})
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    with pytest.raises(ReconnectError):
        run(env, session, cycle_probes(fault_time(env)), said=said)
    assert env.commands.calls.count(list(CONNECT)) == 3
    assert [row["step"] for row in rows_of(session, "cleanup_failed")] == ["reconnect"]
    assert read_json(session.directory / "partition-1.json")["outcome"] == "reconnect_failed"
    assert " ".join(CONNECT) in said[0]


def test_cleanup_does_nothing_when_the_container_was_never_disconnected(tmp_path, monkeypatch):
    """An abort before the disconnect issues no network command, writes no result, and logs the skip."""
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
        run(env, session, FakeProbes({}, []))
    assert list(DISCONNECT) not in env.commands.calls and list(CONNECT) not in env.commands.calls
    assert "abort_cleanup_skipped" in kinds(session)
    assert not (session.directory / "partition-1.json").exists()


def test_cleanup_skips_the_connect_when_the_container_is_attached_again(tmp_path):
    """After an abort the inspect shows the network; no second connect is issued."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED])
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(RuntimeError):
        run(env, session, FailingProbes(RuntimeError("boom")))
    assert list(CONNECT) not in env.commands.calls
    assert "abort_cleanup_skipped" in kinds(session)


def test_cleanup_with_an_unreadable_inspect_logs_the_unknown_state_then_connects(tmp_path):
    """An unreadable inspect is logged as state unknown before the connect is attempted."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, (1, "", "gone")])
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(RuntimeError):
        run(env, session, FailingProbes(RuntimeError("boom")))
    names = kinds(session)
    assert names.index("abort_state_unknown") < names.index("abort_reconnect")
    assert env.commands.calls.count(list(CONNECT)) == 1


def test_symptom_is_rpc_failure_peer_count_or_unknown():
    """Health-check failures win; only low-peer health lines give peer_count; nothing gives unknown."""
    failed = event("health_failed", "validator-1", 1, "a:1")
    low = LogEvent("health_status", "validator-1", 2, "info", "m", "a:2",
                   {"role": "active", "height": 5, "peers": 1, "healthy": False})
    fine = LogEvent("health_status", "validator-1", 3, "info", "m", "a:3",
                    {"role": "active", "height": 6, "peers": 5, "healthy": True})
    assert classify_symptom([low, failed, fine]) == "rpc_failure"
    assert classify_symptom([fine, low]) == "peer_count"
    assert classify_symptom([fine]) == "unknown"
    assert classify_symptom([]) == "unknown"


def test_the_symptom_is_read_from_the_active_log_between_disconnect_and_detection(tmp_path):
    """A health failure logged between the disconnect and the detection gives rpc_failure in the result."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    with env.paths.active_log.open("a", encoding="utf-8") as handle:
        handle.write('time="2026-10-05 10:00:20.000" level=error msg="CometBFT health check failed: refused" '
                     "module=health node=validator-1\n")
    result = run(env, session, cycle_probes(fault_time(env)))
    assert result["detection_symptom"] == "rpc_failure"


def test_series_runs_one_monitor_around_every_iteration(tmp_path):
    """One monitor start and stop for two iterations, and the second iteration is numbered after the first."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    session.events.append("iteration_start", scenario="hang", iteration=4)
    probes = cycle_probes(fault_time(env), rounds=2)
    monitor = FakeMonitor()
    results = run_partition_series(env, session, 2, SETTINGS, probes, monitor, say=print)
    assert [(row["iteration"], row["outcome"]) for row in results] == [(5, "complete"), (6, "complete")]
    assert (monitor.started, monitor.stopped) == (1, 1)
    assert (session.directory / "partition-5.json").exists() and (session.directory / "partition-6.json").exists()


def test_series_stops_on_the_first_outcome_other_than_complete_and_stops_the_monitor(tmp_path):
    """The second iteration is never started after a not_detected outcome; series_aborted names it."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    results = run_partition_series(env, session, 3, SETTINGS, FakeProbes({}, []), monitor, say=print)
    assert [row["outcome"] for row in results] == ["not_detected"]
    assert env.commands.calls.count(list(DISCONNECT)) == 1
    aborted = rows_of(session, "series_aborted")
    assert [(row["iteration"], row["outcome"], row["scenario"]) for row in aborted] == [(1, "not_detected", "partition")]
    assert (monitor.started, monitor.stopped) == (1, 1)


def test_series_stops_the_monitor_when_an_iteration_is_refused(tmp_path):
    """A refusal raises out of the series and the monitor still stops."""
    env = partition_env(tmp_path)
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(PreconditionError):
        run_partition_series(env, session, 2, SETTINGS, FakeProbes({}, []), monitor, say=print)
    assert (monitor.started, monitor.stopped) == (1, 1)


class SampledMonitor(FakeMonitor):
    """FakeMonitor holding samples, so a run can summarize its window."""

    def __init__(self, samples):
        """Keep the samples."""
        super().__init__()
        self.samples = samples

    def samples_since(self, since_ms):
        """The samples at or after since_ms."""
        return [sample for sample in self.samples if sample.t_ms >= since_ms]


def test_indeterminate_seconds_are_reported_beside_an_observed_window(tmp_path):
    """The primary unreachable while the backup holds the key: the window is observed, the seconds are reported."""
    from harness.monitor import UNREACHABLE, MonitorSample
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    fault_ms = fault_time(env)
    down = {"validator1-node": UNREACHABLE, "validator2-node": TARGET_ADDRESS}
    up = {"validator1-node": TARGET_ADDRESS, "validator2-node": "MOCK"}
    samples = [MonitorSample(t_ms, down if fault_ms + 30_000 <= t_ms < fault_ms + 90_000 else up)
               for t_ms in range(fault_ms - 10_000, fault_ms + 400_000, 1000)]
    result = run(env, session, cycle_probes(fault_ms), SampledMonitor(samples))
    assert result["monitor"] == {"observed": True, "observed_problems": [], "dual_signer_seconds": 0,
                                 "indeterminate_seconds": 60}


def test_a_partition_window_without_samples_at_its_end_lists_coded_problems(tmp_path):
    """Samples that stop early are a coded problem with its parameter, rendered in Indonesian only by the report."""
    from harness.monitor import MonitorSample
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    fault_ms = fault_time(env)
    up = {"validator1-node": TARGET_ADDRESS, "validator2-node": "MOCK"}
    samples = [MonitorSample(t_ms, up) for t_ms in range(fault_ms - 10_000, fault_ms + 50_001, 1000)]
    monitor = run(env, session, cycle_probes(fault_ms), SampledMonitor(samples))["monitor"]
    assert monitor["observed"] is False
    assert [problem["code"] for problem in monitor["observed_problems"]] == ["early_last_sample"]
