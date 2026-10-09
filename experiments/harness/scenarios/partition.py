"""partition (III-13): cut the primary off from the localnet's Docker network while its process keeps running.

The primary keeps the validator key in memory, so no key holder disappears; it only loses its peers.
The published RPC port is expected to disappear with the network endpoint, but the runner records
what actually happened: the detection symptom from syncguard's own log, detection, takeover, the
backup's first signature, and, after the container is reconnected with its original address and
aliases, failback. The reconnect always happens, in the normal path or in the abort cleanup, and a
result file is written for every run that issued the disconnect.

Docker publishes a container's host ports only for the networks attached when it starts. When
syncguard restarted validator1-node while it was disconnected, the reconnect brings the network back
but not 21657, and syncguard never sees its node healthy again (Sesi S8). So after the reconnect the
runner checks the host ports and, only when the node provably holds the mock key and is running,
repairs the environment with one `docker restart -t 50 validator1-node` before the failback waits.
"""

from dataclasses import dataclass

from harness.adapters import Unreachable
from harness.constants import (
    BACKUP_NODE,
    MIN_PEERS,
    PRIMARY_CONTAINER,
    PRIMARY_IP,
    PRIMARY_NODE,
    TARGET_VALIDATOR,
    TIP_MARGIN,
)
from harness.jsonio import write_json
from harness.logparse import parse_log_file
from harness.metrics import event_time, seconds_between
from harness.monitor import container_status_argv, status_address, window_report
from harness.preconditions import failover_preconditions
from harness.scenarios.common import (
    abort_recorded,
    best_effort,
    collect_heights_in_chunks,
    container_endpoints,
    container_ports,
    container_status,
    cycle_outcome,
    docker_command,
    follow_failback,
    log_observation,
    missing_host_ports,
    record_window,
    require,
    run_series,
    two_attempts,
)
from harness.scenarios.hang import restart_outcome
from harness.snapshots import key_file_fact

SCENARIO = "partition"
FAULT_KIND = "partition"
RESTART_GRACE_S = 50  # compose stop_grace_period of validator1-node and syncguard's stop_timeout
LOADED_KEY_READS = 3
LOADED_KEY_READ_WAIT_S = 5.0
# Sesi S8: after the disconnected restart every 2 s monitor exec failed for minutes while a read without a timeout answered.
LOADED_KEY_READ_TIMEOUT_S = 15.0
PORT_EVIDENCE_KEYS = ("missing_ports_before", "key_file_address", "loaded_address", "loaded_reads", "loaded_read_ms",
                      "restart_exit_code", "missing_ports_after")


@dataclass(frozen=True)
class PartitionSettings:
    """Waits of one partition iteration.

    detection_timeout_s: 60 s for syncguard to detect the unreachable primary.
    takeover_timeout_s: how long the backup has to log its takeover after the disconnect.
    reconnect_deadline_s: one deadline, counted from the end of the disconnect command, for detection,
    takeover, and the backup's first signature; the container is reconnected at the backup's first
    signature or at the deadline, whichever comes first. Right after the reconnect the host ports are
    checked and, when docker dropped them and the node provably holds the mock key, the container is
    restarted once (the loaded key is read up to 3 times, 5 s apart, each with a 15 s command timeout); the
    failback waits start after that.
    failback_timeout_s: how long failback and the release each may take.
    signature_timeout_s: how long to wait for the primary's first signature after the release.
    post_settle_s: how long the dual-signer monitor keeps running after the reconnect and failback; at
    least the 90 s role-change spacing so the next run's precondition holds.
    """

    detection_timeout_s: float = 60.0
    takeover_timeout_s: float = 180.0
    reconnect_deadline_s: float = 180.0
    failback_timeout_s: float = 300.0
    signature_timeout_s: float = 180.0
    post_settle_s: float = 100.0
    heights_before_fault: int = 30


RESULT_KEYS = (
    "outcome", "network", "t_fault_ms", "fault_height", "fault_exit_code", "command", "detected", "ttd_s",
    "detection_source", "detection_symptom", "takeover", "takeover_source", "backup_first_signature_height", "ttr_s",
    "restart_outcome", "restart_source", "restart_before_takeover", "restart_evidence", "container_state_before_reconnect", "attached_before_reconnect",
    "reconnect_ms", "reconnect_exit_code", "port_repair", "port_repair_ms", "port_repair_evidence",
    "failback_complete", "failback_source",
    "primary_first_signature_height", "final_container_state", "final_attached", "final_ip", "error",
)


class ReconnectError(Exception):
    """The reconnect command failed or the primary is not back at its original address; the cleanup retries."""


def connect_argv(endpoint: dict) -> list:
    """`docker network connect --ip <ip> [--alias <a> ...] <network> validator1-node`: the original endpoint."""
    aliases = [part for alias in endpoint["aliases"] for part in ("--alias", alias)]
    return ["docker", "network", "connect", "--ip", endpoint["ip"], *aliases, endpoint["network"], PRIMARY_CONTAINER]


def disconnect_argv(endpoint: dict) -> list:
    """The exact `docker network disconnect <network> validator1-node` command."""
    return ["docker", "network", "disconnect", endpoint["network"], PRIMARY_CONTAINER]


def primary_endpoint(env) -> tuple:
    """(endpoint, failures): the primary's single network endpoint, or every reason a partition cannot be made.

    The disconnect isolates the node only when it has one network, and the reconnect restores the
    compose address, so the node must be on it now.
    """
    endpoints = container_endpoints(env, PRIMARY_CONTAINER)
    if endpoints is None:
        return None, [f"cannot read the networks of {PRIMARY_CONTAINER} from docker inspect"]
    if len(endpoints) != 1:
        return None, [f"{PRIMARY_CONTAINER} is on {len(endpoints)} networks ({', '.join(sorted(endpoints))}); "
                      "a partition needs exactly one"]
    network, details = next(iter(endpoints.items()))
    failures = []
    if details["address"] != PRIMARY_IP:
        failures.append(f"{PRIMARY_CONTAINER} is at {details['address']} on {network}, expected the compose address {PRIMARY_IP}")
    ip = details["ip"] or details["address"]
    return {"network": network, "ip": ip, "aliases": details["aliases"]}, failures


def endpoint_state(env, endpoint: dict) -> dict:
    """The primary's container state, whether it is attached to the network, and its address there (None if unknown)."""
    endpoints = container_endpoints(env, PRIMARY_CONTAINER)
    attached = None if endpoints is None else endpoint["network"] in endpoints
    return {"container_state": container_status(env, PRIMARY_CONTAINER), "attached": attached,
            "networks": None if endpoints is None else sorted(endpoints),
            "address": endpoints[endpoint["network"]]["address"] if attached else None}


def is_restored(state: dict, endpoint: dict) -> bool:
    """Whether the primary is attached to its network at the original address."""
    return state["attached"] is True and state["address"] == endpoint["ip"]


def restart_precedes_takeover(restart, takeover) -> bool | None:
    """Safety evidence: whether syncguard's restart (or stop confirmation) of the primary was logged before the takeover.

    None when there was no takeover to compare with; False when there was a takeover and no restart
    marker, or the restart was logged after it. The two log clocks are the same host-converted clock.
    """
    if takeover is None:
        return None
    return restart is not None and restart.t_ms <= takeover.t_ms


def earliest_restart_evidence(probes, since_ms: int):
    """The earlier of the primary's 'Container ... restarted' and 'Node stopped after the failed restart' lines, or None."""
    found = [probes.marker(PRIMARY_NODE, marker, since_ms, 0) for marker in ("container_restarted", "stopped_after_failed_restart")]
    return min((event for event in found if event is not None), key=lambda event: event.t_ms, default=None)


TIMEOUT_TEXTS = ("Client.Timeout exceeded", "context deadline exceeded", "i/o timeout")
REFUSED_TEXT = "connection refused"


def classify_rpc_failures(failures: list) -> str:
    """The RPC symptom from the health_failed events: rpc_timeout, rpc_refused, or rpc_failure.

    rpc_timeout when any error text holds a Go timeout wording; otherwise rpc_refused when every
    error text holds "connection refused"; otherwise rpc_failure (EOF, reset, bad status, no text).
    """
    details = [str(event.details.get("detail", "")) for event in failures]
    if any(text in detail for detail in details for text in TIMEOUT_TEXTS):
        return "rpc_timeout"
    if all(REFUSED_TEXT in detail for detail in details):
        return "rpc_refused"
    return "rpc_failure"


def classify_symptom(events: list, min_peers: int = MIN_PEERS) -> str:
    """How syncguard saw the partition, from validator-1's log events between the disconnect and the detection.

    Any `CometBFT health check failed` line gives the RPC class of classify_rpc_failures; otherwise
    peer_count when health lines with peers below min_peers (and healthy false) exist, else unknown.
    """
    failures = [event for event in events if event.marker == "health_failed"]
    if failures:
        return classify_rpc_failures(failures)
    for event in events:
        if event.marker == "health_status" and not event.details["healthy"] and event.details["peers"] < min_peers:
            return "peer_count"
    return "unknown"


def detection_symptom(env, since_ms: int, detection) -> str | None:
    """The symptom of a detected partition, read from the active syncguard log; None when nothing was detected."""
    if detection is None:
        return None
    path = env.paths.active_log
    events = parse_log_file(path, PRIMARY_NODE, env.utc_offset_seconds, since_ms, detection.t_ms) if path.exists() else []
    return classify_symptom([event for event in events if event.node == PRIMARY_NODE])


def reconnect_if_left_disconnected(env, session, iteration: int, endpoint: dict, progress: dict) -> None:
    """Abort cleanup: connect the primary back with its original address and aliases when it is not attached.

    Nothing is issued when the disconnect never happened or the reconnect already succeeded. An
    unreadable inspect is logged as state unknown before the connect; the inspect, each connect, and
    the verification survive a second interrupt (best effort). The connect is tried twice, and
    cleanup_failed is logged with the step when it did not complete or the address is wrong.
    """
    if not progress["needs_reconnect"]:
        session.events.append("abort_cleanup_skipped", scenario=SCENARIO, iteration=iteration, **progress)
        return
    inspected, endpoints = best_effort(session, SCENARIO, iteration, "inspect",
                                       lambda: container_endpoints(env, PRIMARY_CONTAINER))
    if not inspected or endpoints is None:
        session.events.append("abort_state_unknown", scenario=SCENARIO, iteration=iteration,
                              note="the networks of the primary could not be read; connecting anyway")
    elif endpoint["network"] in endpoints:
        session.events.append("abort_cleanup_skipped", scenario=SCENARIO, iteration=iteration,
                              container_networks=sorted(endpoints), **progress)
        progress["needs_reconnect"] = False
        return
    connected = two_attempts(session, SCENARIO, iteration, "reconnect", lambda attempt: docker_command(
        env, session, connect_argv(endpoint), "network connect", PRIMARY_CONTAINER, SCENARIO, iteration,
        "abort_reconnect", network=endpoint["network"], attempt=attempt))
    if connected is None:
        return
    verified, state = best_effort(session, SCENARIO, iteration, "verify", lambda: endpoint_state(env, endpoint))
    restored = verified and is_restored(state, endpoint)
    session.events.append("abort_reconnect_verified", scenario=SCENARIO, iteration=iteration, restored=restored)
    if restored:
        progress["needs_reconnect"] = False
    else:
        session.events.append("cleanup_failed", scenario=SCENARIO, iteration=iteration, step="verify",
                              error="the primary is not attached at its original address after the connect")


def outcome_of_error(error: BaseException) -> str:
    """The result outcome of a run that ended in an exception after the disconnect."""
    if isinstance(error, ReconnectError):
        return "reconnect_failed"
    if isinstance(error, KeyboardInterrupt):
        return "interrupted"
    return "aborted"


def new_result(endpoint: dict) -> dict:
    """A result with every key present and nothing measured yet."""
    result = dict.fromkeys(RESULT_KEYS)
    result.update(network=endpoint["network"], detected=False, takeover=False, failback_complete=False)
    return result


def record_final_state(env, endpoint: dict, result: dict) -> dict:
    """Store the primary's container state, attachment, and address as they are now; return the state."""
    state = endpoint_state(env, endpoint)
    result.update(final_container_state=state["container_state"], final_attached=state["attached"],
                  final_ip=state["address"])
    return state


def operator_message(endpoint: dict, result: dict) -> str | None:
    """What the operator must do when the primary is not back at its original address, or None when it is."""
    if result["final_attached"] is True and result["final_ip"] == endpoint["ip"]:
        return None
    connect = " ".join(connect_argv(endpoint))
    if result["final_attached"] is True:
        disconnect = " ".join(disconnect_argv(endpoint))
        return (f"{PRIMARY_CONTAINER} is attached to {endpoint['network']} at {result['final_ip']}, "
                f"not {endpoint['ip']}. Run: {disconnect} && {connect}")
    return f"{PRIMARY_CONTAINER} may still be disconnected from {endpoint['network']}. Run: {connect}"


def restart_argv() -> list:
    """`docker restart -t 50 validator1-node`: the grace matches the compose stop_grace_period and syncguard's stop_timeout."""
    return ["docker", "restart", "-t", str(RESTART_GRACE_S), PRIMARY_CONTAINER]


def answered_address(result) -> str | None:
    """The loaded address of one in-container /status read, or None when the read gave no non-empty address."""
    try:
        address = status_address(result)
    except Unreachable:
        return None
    if isinstance(address, str) and address:
        return address
    return None


def read_loaded_key(env) -> dict:
    """The address validator1-node loaded, read inside the container up to 3 times 5 s apart, each with a 15 s timeout.

    Gives loaded_address (None when no read answered with an address), loaded_reads (the reads made),
    and loaded_read_ms (how long the answering read took, else None). The monitor keeps its own 2 s read.
    """
    for attempt in range(1, LOADED_KEY_READS + 1):
        result = env.commands.run(container_status_argv(PRIMARY_CONTAINER), timeout=LOADED_KEY_READ_TIMEOUT_S)
        address = answered_address(result)
        if address is not None:
            return {"loaded_address": address, "loaded_reads": attempt,
                    "loaded_read_ms": result.ended_ms - result.started_ms}
        if attempt < LOADED_KEY_READS:
            env.clock.sleep(LOADED_KEY_READ_WAIT_S)
    return {"loaded_address": None, "loaded_reads": LOADED_KEY_READS, "loaded_read_ms": None}


def mock_key_refusal(key_file_address: str | None, loaded_address: str | None) -> str | None:
    """Why the primary may hold the validator key (so the runner must not restart it), or None when it provably does not.

    A restart loads the key file, so an unreadable key file or one naming the validator refuses the
    restart; so does a loaded key that could not be read or is the validator's.
    """
    if key_file_address is None:
        return "the key file of validator1 has no readable address"
    if key_file_address == TARGET_VALIDATOR:
        return "the key file of validator1 holds the validator key"
    if loaded_address is None:
        return "the key validator1 has loaded could not be read"
    if loaded_address == TARGET_VALIDATOR:
        return "validator1 has the validator key loaded"
    return None


def port_repair_message(state: str, missing_ports: list | None) -> str:
    """The operator line when the host ports were not repaired: the state, the ports, and what to check before a restart."""
    if missing_ports is None:
        detail = f"the host ports of {PRIMARY_CONTAINER} could not be read after the reconnect"
    else:
        detail = f"{PRIMARY_CONTAINER} lacks host ports after the reconnect ({', '.join(missing_ports)})"
    return (f"Host port repair {state}: {detail}; syncguard on validator1 may not reach its node. "
            f"Before a manual {' '.join(restart_argv())}, check that the container is running and syncguard did not "
            "stop it on purpose, and that priv_validator_key.json and the loaded key (/status validator_info.address) "
            f"both hold the mock key, not {TARGET_VALIDATOR[:8]}.")


def run_partition(env, session, iteration: int, settings: PartitionSettings, probes, monitor, say=print) -> dict:
    """One partition iteration; the caller runs the dual-signer monitor, this reads its samples for the window.

    The result JSON is written before the final heights are collected, and also for any run that issued
    the disconnect and then failed (outcome reconnect_failed, aborted, or interrupted, with every field
    measured so far); the error is then re-raised. An abort reconnects the container in its cleanup.
    say prints the operator message when the container may still be disconnected, and the host-port
    line when the ports were missing or unreadable after the reconnect and the runner did not repair them.
    """
    started_ms = env.clock.now_ms()
    failures = failover_preconditions(env)
    endpoint, network_failures = primary_endpoint(env)
    require(session, SCENARIO, failures + network_failures, iteration)
    session.events.append("iteration_start", scenario=SCENARIO, iteration=iteration)
    progress = {"needs_reconnect": False, "disconnect_attempted": False}
    result = new_result(endpoint)

    def abort_cleanup() -> None:
        """Reconnect a primary left disconnected, then record the syncguard log window."""
        try:
            reconnect_if_left_disconnected(env, session, iteration, endpoint, progress)
        finally:
            record_window(env, session, iteration, started_ms, env.clock.now_ms())

    error = None
    try:
        with abort_recorded(session, SCENARIO, iteration, abort_cleanup):
            first_height = max(1, env.rpc.tip_height() - settings.heights_before_fault)
            _disconnect_and_follow(env, session, iteration, settings, probes, endpoint, progress, result, say)
    except BaseException as caught:  # stored below, then re-raised unchanged
        error = caught
    if error is not None and not progress["disconnect_attempted"]:
        raise error
    if error is not None:
        result["outcome"] = outcome_of_error(error)
        result["error"] = repr(error)
        best_effort(session, SCENARIO, iteration, "final state", lambda: record_final_state(env, endpoint, result))
        message = operator_message(endpoint, result)
        if message is not None:
            say(message)
    result["monitor"] = _monitor_summary(monitor.samples_since(started_ms), iteration,
                                         result["t_fault_ms"] or started_ms, env.clock.now_ms())
    result = {"session": session.id, "iteration": iteration, **result}
    if error is None:
        record_window(env, session, iteration, started_ms, env.clock.now_ms())
    write_json(session.directory / f"partition-{iteration}.json", result)
    if error is not None:
        raise error
    session.events.append("iteration_result", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
    with abort_recorded(session, SCENARIO, iteration):
        collect_heights_in_chunks(env, session, first_height, env.rpc.tip_height() - TIP_MARGIN)
    session.events.append("iteration_end", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
    return result


def _monitor_summary(samples: list, iteration: int, fault_ms: int, end_ms: int) -> dict:
    """The monitor fields of the result: the run window [fault, end] under the one coverage rule of monitor.py.

    Indeterminate seconds are reported beside the observed flag; they do not change it.
    """
    window = {"scenario": SCENARIO, "iteration": iteration, "start_ms": fault_ms, "end_ms": end_ms}
    report = window_report(samples, window, TARGET_VALIDATOR)
    return {"observed": report["observed"], "observed_problems": report["problems"],
            "dual_signer_seconds": report["dual_signer_seconds"],
            "indeterminate_seconds": report["indeterminate_seconds"]}


def _remaining_s(env, deadline_ms: int) -> float:
    """Seconds left until deadline_ms, never negative."""
    return max(0.0, (deadline_ms - env.clock.now_ms()) / 1000)


def _disconnect_and_follow(env, session, iteration: int, settings: PartitionSettings, probes, endpoint: dict,
                           progress: dict, result: dict, say) -> None:
    """Disconnect the primary, follow detection and takeover, reconnect, repair its host ports, follow failback, settle.

    result is filled in as each step finishes, so an abort leaves what was measured. Log markers are
    searched from the instant the disconnect was issued; signatures only from the instant it ended.
    A primary that was never detected is still reconnected and its host ports still checked.
    """
    fault_height = env.rpc.tip_height()
    progress["needs_reconnect"] = True
    progress["disconnect_attempted"] = True
    disconnect = docker_command(env, session, disconnect_argv(endpoint), "network disconnect", PRIMARY_CONTAINER,
                                SCENARIO, iteration, "fault_injected", height=fault_height, fault_kind=FAULT_KIND,
                                network=endpoint["network"])
    result.update(t_fault_ms=disconnect.started_ms, fault_height=fault_height, fault_exit_code=disconnect.exit_code,
                  command=" ".join(disconnect.argv))
    if disconnect.exit_code != 0 and _command_failed_for_good(env, session, iteration, disconnect, endpoint):
        progress["needs_reconnect"] = False
        result["outcome"] = "fault_command_failed"
        return
    fault_ms = disconnect.started_ms
    deadline_ms = disconnect.ended_ms + round(settings.reconnect_deadline_s * 1000)
    detection = probes.marker(PRIMARY_NODE, "max_failures", fault_ms,
                              min(settings.detection_timeout_s, _remaining_s(env, deadline_ms)))
    log_observation(session, iteration, "max_failures", detection)
    result.update(detected=detection is not None, ttd_s=seconds_between(fault_ms, event_time(detection)),
                  detection_source=detection.source if detection is not None else None,
                  detection_symptom=detection_symptom(env, fault_ms, detection))
    takeover_wait_s = min(settings.takeover_timeout_s, _remaining_s(env, deadline_ms)) if detection is not None else 0
    takeover = probes.marker(BACKUP_NODE, "takeover_complete", fault_ms, takeover_wait_s)
    if detection is not None:
        log_observation(session, iteration, "takeover_complete", takeover)
    result.update(takeover=takeover is not None, takeover_source=takeover.source if takeover is not None else None)
    backup_signature = None
    if takeover is not None:
        backup_signature = probes.signature(disconnect.ended_ms, fault_height, _remaining_s(env, deadline_ms))
        log_observation(session, iteration, "backup_first_signature", backup_signature)
        if backup_signature is not None:
            result.update(backup_first_signature_height=backup_signature.height,
                          ttr_s=seconds_between(fault_ms, backup_signature.signers[TARGET_VALIDATOR]))
    _reconnect(env, session, iteration, endpoint, progress, result)
    _repair_host_ports(env, session, iteration, endpoint, progress, result, say)
    failback, released, primary_signature = follow_failback(session, iteration, probes, takeover, fault_height,
                                                            settings.failback_timeout_s, settings.signature_timeout_s)
    result.update(failback_complete=failback is not None, failback_source=failback.source if failback else None,
                  primary_first_signature_height=primary_signature.height if primary_signature else None)
    session.events.append("post_recovery_monitoring", iteration=iteration, seconds=settings.post_settle_s)
    env.clock.sleep(settings.post_settle_s)
    restart, restart_source = restart_outcome(probes, fault_ms)
    result.update(restart_outcome=restart, restart_source=restart_source)
    restart_evidence = earliest_restart_evidence(probes, fault_ms)
    result.update(restart_before_takeover=restart_precedes_takeover(restart_evidence, takeover),
                  restart_evidence={"restart_source": restart_evidence.source if restart_evidence else None,
                                    "takeover_source": takeover.source if takeover else None})
    record_final_state(env, endpoint, result)
    result["outcome"] = _outcome(detection, takeover, backup_signature, failback, released, primary_signature)


def _command_failed_for_good(env, session, iteration: int, disconnect, endpoint: dict) -> bool:
    """After a failed disconnect: True when the container is provably still attached (nothing to follow).

    The state is logged. When docker cannot say, the run cannot tell whether the partition exists, so it
    raises and the abort cleanup reconnects.
    """
    state = endpoint_state(env, endpoint)
    session.events.append("fault_command_failed", iteration=iteration, exit_code=disconnect.exit_code,
                          container_state=state["container_state"], container_networks=state["networks"])
    if state["attached"] is None:
        raise RuntimeError("cannot confirm whether the disconnect took effect: docker inspect is unreadable")
    return state["attached"]


def _reconnect(env, session, iteration: int, endpoint: dict, progress: dict, result: dict) -> None:
    """Connect the primary back with its original address and aliases, and verify it is attached there.

    The state before is recorded (syncguard's restart may have left it attached already, in which case
    no connect is issued). A failed command or a different address raises ReconnectError.
    """
    before = endpoint_state(env, endpoint)
    result.update(container_state_before_reconnect=before["container_state"], attached_before_reconnect=before["attached"])
    if before["attached"] is True:
        result.update(reconnect_ms=env.clock.now_ms(), reconnect_exit_code=None)
    else:
        connect = docker_command(env, session, connect_argv(endpoint), "network connect", PRIMARY_CONTAINER, SCENARIO,
                                 iteration, "fault_cleared", fault_kind=FAULT_KIND, network=endpoint["network"])
        result.update(reconnect_ms=connect.started_ms, reconnect_exit_code=connect.exit_code)
        if connect.exit_code != 0:
            raise ReconnectError(f"docker network connect exited {connect.exit_code}: {connect.stderr.strip()}")
    after = endpoint_state(env, endpoint)
    if not is_restored(after, endpoint):
        raise ReconnectError(f"after the connect the primary is attached={after['attached']} at {after['address']}, "
                             f"expected {endpoint['ip']}")
    progress["needs_reconnect"] = False


def _repair_host_ports(env, session, iteration: int, endpoint: dict, progress: dict, result: dict, say) -> None:
    """After the reconnect: check the primary's host ports and republish them by a restart when that is provably safe.

    One port_check event records the missing ports (or that they are unreadable). The first rule that
    applies sets port_repair: unreadable ports unknown; nothing missing not_needed; a container that is
    not running not_running (syncguard may have stopped it on purpose, and the runner never starts it);
    a key file or loaded key that is unreadable or 48DC2183 refused (a restart loads the key file);
    otherwise the restart of _restart_for_ports. Every state but not_needed is logged as a port_repair
    event with the evidence, and every state but not_needed and restarted is told to the operator.
    The run continues to the failback waits whatever the state. While the decision is pending,
    port_repair is decision_unfinished, which a run that ends before the decision keeps.
    """
    evidence = dict.fromkeys(PORT_EVIDENCE_KEYS)
    result.update(port_repair_evidence=evidence)
    ports = container_ports(env, PRIMARY_CONTAINER)
    missing = None if ports is None else missing_host_ports(ports)
    evidence["missing_ports_before"] = missing
    session.events.append("port_check", scenario=SCENARIO, iteration=iteration, readable=ports is not None,
                          missing_ports=missing)
    if missing == []:
        result["port_repair"] = "not_needed"
        return
    # The key reads (3 of up to 15 s, 5 s apart) can take 55 s; a run that ends during them keeps this state.
    result["port_repair"] = "decision_unfinished"

    def record(state: str, reason: str | None = None) -> None:
        """Store the state, log it with the evidence, and tell the operator when the ports were not repaired."""
        result["port_repair"] = state
        session.events.append("port_repair", scenario=SCENARIO, iteration=iteration, state=state, reason=reason,
                              **evidence)
        if state != "restarted":
            say(port_repair_message(state, missing))

    if missing is None:
        record("unknown")
        return
    if container_status(env, PRIMARY_CONTAINER) != "running":
        record("not_running")
        return
    evidence["key_file_address"] = key_file_fact(env.paths.primary_key).get("address")
    evidence.update(read_loaded_key(env))
    refusal = mock_key_refusal(evidence["key_file_address"], evidence["loaded_address"])
    if refusal is not None:
        record("refused", refusal)
        return
    _restart_for_ports(env, session, iteration, endpoint, progress, evidence, result, record)


def _restart_for_ports(env, session, iteration: int, endpoint: dict, progress: dict, evidence: dict, result: dict,
                       record) -> None:
    """`docker restart -t 50 validator1-node`, then the host ports and the endpoint read again; record(state) stores it.

    restart_failed when the command exits non-zero; otherwise unknown (ports unreadable), still_missing, or
    restarted. The restart could change the attachment, so the abort cleanup reconnects until the endpoint
    is verified again, and a primary that is not back at its original address raises ReconnectError after
    the state is recorded (the same rule as _reconnect). Before the command is issued, port_repair becomes
    restart_unfinished with the issue time and a port_republish_issued event, so a run that ends while the
    restart runs still shows it was issued; port_repair_ms then takes the command's own start.
    """
    progress["needs_reconnect"] = True
    decision = {"missing_ports": evidence["missing_ports_before"], "key_file_address": evidence["key_file_address"],
                "loaded_address": evidence["loaded_address"]}
    # docker_command logs only after the command returns (up to the 50 s grace), so the issue is recorded first.
    result.update(port_repair="restart_unfinished", port_repair_ms=env.clock.now_ms())
    session.events.append("port_republish_issued", t_ms=result["port_repair_ms"], scenario=SCENARIO,
                          iteration=iteration, command=" ".join(restart_argv()), **decision)
    restart = docker_command(env, session, restart_argv(), "restart", PRIMARY_CONTAINER, SCENARIO, iteration,
                             "port_republish", **decision)
    result["port_repair_ms"] = restart.started_ms
    evidence["restart_exit_code"] = restart.exit_code
    if restart.exit_code != 0:
        record("restart_failed")
        return
    ports = container_ports(env, PRIMARY_CONTAINER)
    after = endpoint_state(env, endpoint)
    missing_after = None if ports is None else missing_host_ports(ports)
    evidence["missing_ports_after"] = missing_after
    if missing_after is None:
        record("unknown")
    elif missing_after:
        record("still_missing")
    else:
        record("restarted")
    if not is_restored(after, endpoint):
        raise ReconnectError(f"after the port restart the primary is attached={after['attached']} at "
                             f"{after['address']}, expected {endpoint['ip']}")
    progress["needs_reconnect"] = False


def _outcome(detection, takeover, backup_signature, failback, released, primary_signature) -> str:
    """not_detected when syncguard never detected the partition, otherwise the outcome of its cycle."""
    if detection is None:
        return "not_detected"
    return cycle_outcome(takeover, backup_signature, failback, released, primary_signature)


def run_partition_series(env, session, count: int, settings: PartitionSettings, probes, monitor, say=print) -> list:
    """Run count iterations with one dual-signer monitor around all of them (common.run_series).

    Any outcome other than complete stops the series, logged as series_aborted; a refusal or an abort
    ends it by raising. Iterations that completed before it stay valid.
    """

    def run_one(iteration: int) -> dict:
        """One partition run that reads the series monitor's samples."""
        return run_partition(env, session, iteration, settings, probes, monitor, say)

    return run_series(session, SCENARIO, count, monitor, run_one, lambda result: result["outcome"])
