"""partition runner: docker network disconnect of the primary, detection, takeover, reconnect, failback."""

import json

import pytest

from fakes import MOCK_ADDRESS, NOT_A_KEY, FakeMonitor, FakeProbes, TARGET_ADDRESS, make_session, ready_env, write_key
from harness.adapters import Unreachable
from harness.jsonio import read_json
from harness.logparse import LogEvent
from harness.scenarios.common import PreconditionError, container_endpoints, container_networks
from harness.scenarios.partition import (
    RESULT_KEYS,
    PartitionSettings,
    ReconnectError,
    classify_symptom,
    restart_precedes_takeover,
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
INSPECT_PORTS = ("docker", "inspect", "validator1-node", "--format",
                 '{"bindings":{{json .HostConfig.PortBindings}},"published":{{json .NetworkSettings.Ports}}}')
RPC_BINDING = {"26657/tcp": [{"HostIp": "", "HostPort": "21657"}]}
RPC_PUBLISHED = {"26657/tcp": [{"HostIp": "0.0.0.0", "HostPort": "21657"}, {"HostIp": "::", "HostPort": "21657"}]}
PORTS_PRESENT = (0, json.dumps({"bindings": RPC_BINDING, "published": RPC_PUBLISHED}) + "\n", "")
PORTS_MISSING = (0, json.dumps({"bindings": RPC_BINDING, "published": {}}) + "\n", "")
MISSING_RPC = ["26657/tcp->21657"]
RESTART = ("docker", "restart", "-t", "50", "validator1-node")
LOADED_KEY = ("docker", "exec", "validator1-node", "wget", "-qO-", "http://127.0.0.1:26657/status")
RESTARTED = (0, "validator1-node\n", "")


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
    The host-port inspect shows 21657 published, so no repair is needed unless a test scripts otherwise.
    """
    commands = {INSPECT_NETWORKS: inspect_answers or [ATTACHED, DETACHED, ATTACHED],
                INSPECT_STATE: [(0, "running\n", "")], DISCONNECT: [(0, "", "")], CONNECT: [(0, "", "")],
                INSPECT_PORTS: [PORTS_PRESENT], **(script or {})}
    return ready_env(tmp_path, extra_script=commands, durations=durations)


def loaded_status(address: str) -> tuple:
    """A docker exec answer of /status whose loaded validator address is address."""
    return 0, json.dumps({"result": {"validator_info": {"address": address}}}), ""


def repair_env(tmp_path, inspect_answers=None, overrides=None):
    """partition_env with 21657 dropped after the reconnect and back after a restart; the node loaded the mock key.

    overrides replaces any of these answers by argv.
    """
    answers = {INSPECT_PORTS: [PORTS_MISSING, PORTS_PRESENT], LOADED_KEY: [loaded_status(MOCK_ADDRESS)],
               RESTART: [RESTARTED], **(overrides or {})}
    return partition_env(tmp_path, inspect_answers=inspect_answers, script=answers)


class KeySwapProbes(FakeProbes):
    """cycle_probes answers; asking for the takeover writes the mock key into validator1's key file, as syncguard does.

    restart_before_failback records whether the restart command was issued before the failback wait began.
    """

    def __init__(self, env, fault_ms: int):
        """Answer like cycle_probes and remember the primary key path and the command log."""
        complete = cycle_probes(fault_ms)
        super().__init__(complete.markers, complete.signatures)
        self.key_path = env.paths.primary_key
        self.commands = env.commands
        self.restart_before_failback = None

    def marker(self, node, marker, since_ms, timeout_s):
        """Swap the key file to the mock key on the takeover wait, note the restart on the failback wait, then answer."""
        if marker == "takeover_complete":
            write_key(self.key_path, MOCK_ADDRESS)
        if marker == "failback_complete":
            self.restart_before_failback = list(RESTART) in self.commands.calls
        return super().marker(node, marker, since_ms, timeout_s)


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
        "fault_cleared", "port_check", "observed", "observed", "observed", "post_recovery_monitoring",
        "iteration_result", "heights_collected", "iteration_end",
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
    assert result["restart_before_takeover"] is True
    assert result["restart_evidence"] == {"restart_source": "syncguard.log:85", "takeover_source": "passive:12"}
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
    assert kinds(session)[-7:] == ["wait_timeout", "fault_cleared", "port_check", "post_recovery_monitoring",
                                  "iteration_result", "heights_collected", "iteration_end"]


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


def failed_with(detail, t_ms=1):
    """A health_failed event carrying the error text, as logparse extracts it."""
    return LogEvent("health_failed", "validator-1", t_ms, "error", "m", f"a:{t_ms}", {"detail": detail})


REFUSED = 'failed to query CometBFT: Get "http://localhost:21657/status": dial tcp [::1]:21657: connect: connection refused'
TIMEOUT = 'Get "http://localhost:21657/status": (Client.Timeout exceeded while awaiting headers)'


def test_symptom_is_peer_count_or_unknown_without_rpc_failures():
    """Only low-peer health lines give peer_count; nothing gives unknown."""
    low = LogEvent("health_status", "validator-1", 2, "info", "m", "a:2",
                   {"role": "active", "height": 5, "peers": 1, "healthy": False})
    fine = LogEvent("health_status", "validator-1", 3, "info", "m", "a:3",
                    {"role": "active", "height": 6, "peers": 5, "healthy": True})
    assert classify_symptom([fine, low]) == "peer_count"
    assert classify_symptom([fine]) == "unknown"
    assert classify_symptom([]) == "unknown"


def test_every_refused_failure_gives_rpc_refused():
    """Connection refused on every failed check is the crash-class symptom."""
    assert classify_symptom([failed_with(REFUSED, 1), failed_with(REFUSED, 2)]) == "rpc_refused"


@pytest.mark.parametrize("detail", [TIMEOUT, "context deadline exceeded", "dial tcp 10.0.0.21:26657: i/o timeout"])
def test_a_timeout_text_gives_rpc_timeout(detail):
    """Each Go timeout wording is the hang-class symptom."""
    assert classify_symptom([failed_with(detail)]) == "rpc_timeout"


def test_a_timeout_after_refusals_gives_rpc_timeout():
    """One timeout among refusals decides rpc_timeout."""
    assert classify_symptom([failed_with(REFUSED, 1), failed_with(TIMEOUT, 2)]) == "rpc_timeout"


def test_other_error_text_gives_rpc_failure():
    """EOF after a refusal is neither all-refused nor a timeout."""
    assert classify_symptom([failed_with(REFUSED, 1), failed_with("EOF", 2)]) == "rpc_failure"
    assert classify_symptom([failed_with("unexpected status 503")]) == "rpc_failure"


def test_a_health_failed_event_without_detail_gives_rpc_failure():
    """A failure with no error text cannot be shown to be refused."""
    assert classify_symptom([event("health_failed", "validator-1", 1, "a:1")]) == "rpc_failure"


def test_the_symptom_is_read_from_the_active_log_between_disconnect_and_detection(tmp_path):
    """A refused health failure logged between the disconnect and the detection gives rpc_refused in the result."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    with env.paths.active_log.open("a", encoding="utf-8") as handle:
        handle.write('time="2026-10-05 10:00:20.000" level=error msg="CometBFT health check failed: dial tcp: connect: connection refused" '
                     "module=health node=validator-1\n")
    result = run(env, session, cycle_probes(fault_time(env)))
    assert result["detection_symptom"] == "rpc_refused"


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


def test_restart_precedes_takeover_compares_the_two_log_times():
    """True when syncguard's restart (or stop confirmation) is logged before the takeover, else False; None without a takeover."""
    restart = event("container_restarted", "validator-1", 1000, "syncguard.log:85")
    takeover = event("takeover_complete", "validator-2", 2000, "passive:12")
    later = event("container_restarted", "validator-1", 3000, "syncguard.log:99")
    assert restart_precedes_takeover(restart, takeover) is True
    assert restart_precedes_takeover(later, takeover) is False
    assert restart_precedes_takeover(None, takeover) is False
    assert restart_precedes_takeover(restart, None) is None


def test_the_stop_confirmation_counts_as_the_restart_evidence(tmp_path):
    """Without 'Container ... restarted', 'Node stopped after the failed restart' is the earlier-of evidence."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    fault_ms = fault_time(env)
    probes = cycle_probes(fault_ms)
    del probes.markers[("validator-1", "container_restarted")]
    probes.markers[("validator-1", "stopped_after_failed_restart")] = event(
        "stopped_after_failed_restart", "validator-1", fault_ms + 45000, "syncguard.log:88")
    result = run(env, session, probes)
    assert result["restart_before_takeover"] is True
    assert result["restart_evidence"]["restart_source"] == "syncguard.log:88"


NO_EVIDENCE = {"missing_ports_before": None, "key_file_address": None, "loaded_address": None,
               "restart_exit_code": None, "missing_ports_after": None}


def operator_line(said: list, state: str) -> str:
    """The one operator line of a repair that did not happen; it names the state, the ports, and the manual command."""
    assert len(said) == 1
    assert state in said[0] and "docker restart -t 50 validator1-node" in said[0]
    return said[0]


def test_published_ports_need_no_repair_and_no_restart(tmp_path):
    """21657 published after the reconnect: not_needed, one port_check event, no restart, no key read, no operator line."""
    env = partition_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    result = run(env, session, cycle_probes(fault_time(env)), said=said)
    assert {"port_repair", "port_repair_ms", "port_repair_evidence"} <= set(RESULT_KEYS)
    assert (result["port_repair"], result["port_repair_ms"]) == ("not_needed", None)
    assert result["port_repair_evidence"] == {**NO_EVIDENCE, "missing_ports_before": []}
    assert list(RESTART) not in env.commands.calls and list(LOADED_KEY) not in env.commands.calls
    check = rows_of(session, "port_check")[0]
    assert (check["readable"], check["missing_ports"], check["scenario"], check["iteration"]) == (True, [], "partition", 1)
    assert rows_of(session, "port_repair") == [] and said == []


def test_missing_ports_with_the_mock_key_restart_the_primary_once_before_the_failback_waits(tmp_path):
    """Mock key in the file and in the node: one docker restart -t 50 after the connect, logged and timed, then failback."""
    env = repair_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    probes = KeySwapProbes(env, fault_time(env))
    said = []
    result = run(env, session, probes, said=said)
    calls = env.commands.calls
    assert calls.count(list(RESTART)) == 1
    assert calls.index(list(CONNECT)) < calls.index(list(RESTART))
    assert probes.restart_before_failback is True
    assert kinds(session) == [
        "preconditions_passed", "iteration_start", "fault_injected", "observed", "observed", "observed",
        "fault_cleared", "port_check", "port_republish", "port_repair", "observed", "observed", "observed",
        "post_recovery_monitoring", "iteration_result", "heights_collected", "iteration_end",
    ]
    republish = rows_of(session, "port_republish")[0]
    assert (republish["command"], republish["exit_code"]) == ("docker restart -t 50 validator1-node", 0)
    assert (republish["missing_ports"], republish["key_file_address"], republish["loaded_address"]) == (
        MISSING_RPC, MOCK_ADDRESS, MOCK_ADDRESS)
    assert (result["port_repair"], result["port_repair_ms"]) == ("restarted", republish["t_ms"])
    assert result["port_repair_evidence"] == {"missing_ports_before": MISSING_RPC, "key_file_address": MOCK_ADDRESS,
                                              "loaded_address": MOCK_ADDRESS, "restart_exit_code": 0,
                                              "missing_ports_after": []}
    assert rows_of(session, "port_repair")[0]["state"] == "restarted"
    assert result["outcome"] == "complete" and said == []
    assert NOT_A_KEY not in (session.directory / "events.jsonl").read_text(encoding="utf-8")
    assert NOT_A_KEY not in (session.directory / "partition-1.json").read_text(encoding="utf-8")


def test_a_key_file_holding_the_validator_key_refuses_the_restart(tmp_path):
    """The key file still names 48DC2183: refused, no restart, the operator is told, and the failback wait still runs."""
    env = repair_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    probes = cycle_probes(fault_time(env))
    said = []
    result = run(env, session, probes, said=said)
    assert list(RESTART) not in env.commands.calls
    assert (result["port_repair"], result["port_repair_ms"]) == ("refused", None)
    assert result["port_repair_evidence"] == {**NO_EVIDENCE, "missing_ports_before": MISSING_RPC,
                                              "key_file_address": TARGET_ADDRESS, "loaded_address": MOCK_ADDRESS}
    decision = rows_of(session, "port_repair")[0]
    assert decision["state"] == "refused" and "key file" in decision["reason"]
    assert (decision["key_file_address"], decision["loaded_address"]) == (TARGET_ADDRESS, MOCK_ADDRESS)
    assert "26657/tcp->21657" in operator_line(said, "refused")
    assert ("marker", "validator-1", "failback_complete") in probes.calls


def test_a_loaded_validator_key_refuses_the_restart(tmp_path):
    """The key file is the mock key but the node still runs 48DC2183: refused, no restart."""
    env = repair_env(tmp_path, overrides={LOADED_KEY: [loaded_status(TARGET_ADDRESS)]})
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    result = run(env, session, KeySwapProbes(env, fault_time(env)), said=said)
    assert list(RESTART) not in env.commands.calls
    assert result["port_repair"] == "refused"
    decision = rows_of(session, "port_repair")[0]
    assert "loaded" in decision["reason"]
    assert (decision["key_file_address"], decision["loaded_address"]) == (MOCK_ADDRESS, TARGET_ADDRESS)
    operator_line(said, "refused")


@pytest.mark.parametrize("answer", [(1, "", "wget: can't connect to remote host"), (0, "not json", ""),
                                    (0, '{"result": {"validator_info": {}}}', ""),
                                    (0, '{"result": {"validator_info": {"address": ""}}}', "")])
def test_a_loaded_key_unreadable_after_three_reads_refuses_the_restart(tmp_path, answer):
    """Three reads 5 s apart without an address: the loaded key is unknown, so the restart is refused."""
    env = repair_env(tmp_path, overrides={LOADED_KEY: [answer]})
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    result = run(env, session, KeySwapProbes(env, fault_time(env)), said=said)
    assert env.commands.calls.count(list(LOADED_KEY)) == 3
    assert env.clock.slept.count(5.0) == 2
    assert list(RESTART) not in env.commands.calls
    assert result["port_repair"] == "refused"
    assert (result["port_repair_evidence"]["key_file_address"], result["port_repair_evidence"]["loaded_address"]) == (
        MOCK_ADDRESS, None)
    operator_line(said, "refused")


def test_the_loaded_key_is_read_again_until_it_answers(tmp_path):
    """The second read answers: one 5 s wait, no third read, and the restart goes ahead."""
    env = repair_env(tmp_path, overrides={LOADED_KEY: [(1, "", "starting"), loaded_status(MOCK_ADDRESS)]})
    session = make_session(env.paths.data_dir, env.clock)
    result = run(env, session, KeySwapProbes(env, fault_time(env)))
    assert env.commands.calls.count(list(LOADED_KEY)) == 2
    assert env.clock.slept.count(5.0) == 1
    assert result["port_repair"] == "restarted"


def test_a_primary_that_is_not_running_is_never_restarted(tmp_path):
    """syncguard may have stopped the node on purpose: not_running, no key read, no restart."""
    env = repair_env(tmp_path, overrides={INSPECT_STATE: [(0, "exited\n", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    result = run(env, session, KeySwapProbes(env, fault_time(env)), said=said)
    assert list(RESTART) not in env.commands.calls and list(LOADED_KEY) not in env.commands.calls
    assert (result["port_repair"], result["port_repair_ms"]) == ("not_running", None)
    assert result["port_repair_evidence"] == {**NO_EVIDENCE, "missing_ports_before": MISSING_RPC}
    assert rows_of(session, "port_repair")[0]["state"] == "not_running"
    operator_line(said, "not_running")


def test_a_restart_that_exits_non_zero_is_restart_failed(tmp_path):
    """The restart command failed: restart_failed with its exit code and issue time; ports are not read again."""
    env = repair_env(tmp_path, overrides={RESTART: [(1, "", "Error response from daemon: boom")]})
    session = make_session(env.paths.data_dir, env.clock)
    probes = KeySwapProbes(env, fault_time(env))
    said = []
    result = run(env, session, probes, said=said)
    republish = rows_of(session, "port_republish")[0]
    assert (result["port_repair"], result["port_repair_ms"]) == ("restart_failed", republish["t_ms"])
    assert result["port_repair_evidence"]["restart_exit_code"] == 1
    assert result["port_repair_evidence"]["missing_ports_after"] is None
    assert env.commands.calls.count(list(INSPECT_PORTS)) == 1
    operator_line(said, "restart_failed")
    assert ("marker", "validator-1", "failback_complete") in probes.calls


def test_ports_still_missing_after_the_restart_are_still_missing(tmp_path):
    """The restart ran but docker still publishes nothing: still_missing with the ports after."""
    env = repair_env(tmp_path, overrides={INSPECT_PORTS: [PORTS_MISSING, PORTS_MISSING]})
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    result = run(env, session, KeySwapProbes(env, fault_time(env)), said=said)
    assert env.commands.calls.count(list(RESTART)) == 1
    assert result["port_repair"] == "still_missing"
    assert result["port_repair_evidence"]["missing_ports_after"] == MISSING_RPC
    operator_line(said, "still_missing")


def test_an_endpoint_lost_after_the_restart_is_reconnect_failed_with_the_partial_result(tmp_path):
    """After the restart the primary is off its network: ReconnectError, the cleanup reconnects, the result is stored."""
    env = repair_env(tmp_path, inspect_answers=[ATTACHED, DETACHED, ATTACHED, DETACHED, DETACHED, ATTACHED])
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(ReconnectError):
        run(env, session, KeySwapProbes(env, fault_time(env)), said=[])
    stored = read_json(session.directory / "partition-1.json")
    assert stored["outcome"] == "reconnect_failed" and IP in stored["error"]
    assert stored["port_repair"] == "restarted"
    assert stored["port_repair_ms"] == rows_of(session, "port_republish")[0]["t_ms"]
    assert stored["port_repair_evidence"]["restart_exit_code"] == 0
    assert env.commands.calls.count(list(CONNECT)) == 2
    assert stored["final_attached"] is True


def test_unreadable_ports_are_unknown_and_restart_nothing(tmp_path):
    """docker cannot say which ports are published: unknown, no restart, and the operator is told."""
    env = partition_env(tmp_path, script={INSPECT_PORTS: [(1, "", "Error: No such object")]})
    session = make_session(env.paths.data_dir, env.clock)
    said = []
    result = run(env, session, cycle_probes(fault_time(env)), said=said)
    assert list(RESTART) not in env.commands.calls
    assert (result["port_repair"], result["port_repair_evidence"]) == ("unknown", NO_EVIDENCE)
    check = rows_of(session, "port_check")[0]
    assert (check["readable"], check["missing_ports"]) == (False, None)
    assert rows_of(session, "port_repair")[0]["state"] == "unknown"
    operator_line(said, "unknown")


def test_a_run_that_never_verified_the_reconnect_has_no_port_repair(tmp_path):
    """A failed reconnect ends the run before the port check: the three fields stay None."""
    env = partition_env(tmp_path, inspect_answers=[ATTACHED, DETACHED], script={CONNECT: [(1, "", "no")]})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(ReconnectError):
        run(env, session, cycle_probes(fault_time(env)), said=[])
    stored = read_json(session.directory / "partition-1.json")
    assert (stored["port_repair"], stored["port_repair_ms"], stored["port_repair_evidence"]) == (None, None, None)
    assert "port_check" not in kinds(session)
