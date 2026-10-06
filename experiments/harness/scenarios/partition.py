"""partition (III-13): cut the primary off from the localnet's Docker network while its process keeps running.

The primary keeps the validator key in memory, so no key holder disappears; it only loses its peers.
The published RPC port is expected to disappear with the network endpoint, but the runner records
what actually happened: the detection symptom from syncguard's own log, detection, takeover, the
backup's first signature, and, after the container is reconnected with its original address and
aliases, failback. The reconnect always happens, in the normal path or in the abort cleanup, and a
result file is written for every run that issued the disconnect.
"""

from dataclasses import dataclass

from harness.constants import (
    BACKUP_NODE,
    MIN_PEERS,
    PRIMARY_CONTAINER,
    PRIMARY_IP,
    PRIMARY_NODE,
    TARGET_VALIDATOR,
    TIP_MARGIN,
)
from harness.finish import monitor_report
from harness.jsonio import write_json
from harness.logparse import parse_log_file
from harness.metrics import event_time, seconds_between
from harness.preconditions import failover_preconditions
from harness.scenarios.common import (
    abort_recorded,
    best_effort,
    collect_heights_in_chunks,
    container_endpoints,
    container_status,
    docker_command,
    log_observation,
    next_iteration,
    record_window,
    require,
)
from harness.scenarios.hang import restart_outcome

SCENARIO = "partition"
FAULT_KIND = "partition"


@dataclass(frozen=True)
class PartitionSettings:
    """Waits of one partition iteration.

    detection_timeout_s: 60 s for syncguard to detect the unreachable primary.
    takeover_timeout_s: how long the backup has to log its takeover after the disconnect.
    reconnect_deadline_s: one deadline, counted from the end of the disconnect command, for detection,
    takeover, and the backup's first signature; the container is reconnected at the backup's first
    signature or at the deadline, whichever comes first.
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
    "restart_outcome", "restart_source", "container_state_before_reconnect", "attached_before_reconnect",
    "reconnect_ms", "reconnect_exit_code", "failback_complete", "failback_source",
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


def classify_symptom(events: list, min_peers: int = MIN_PEERS) -> str:
    """How syncguard saw the partition, from validator-1's log events between the disconnect and the detection.

    rpc_failure when a `CometBFT health check failed` line exists, peer_count when only health lines
    with peers below min_peers (and healthy false) exist, otherwise unknown.
    """
    if any(event.marker == "health_failed" for event in events):
        return "rpc_failure"
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
    for attempt in (1, 2):
        done, result = best_effort(session, SCENARIO, iteration, f"reconnect attempt {attempt}", lambda: docker_command(
            env, session, connect_argv(endpoint), "network connect", PRIMARY_CONTAINER, SCENARIO, iteration,
            "abort_reconnect", network=endpoint["network"], attempt=attempt))
        if done and result.exit_code == 0:
            break
    else:
        session.events.append("cleanup_failed", scenario=SCENARIO, iteration=iteration, step="reconnect",
                              error="the reconnect did not complete after two attempts")
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


def run_partition(env, session, iteration: int, settings: PartitionSettings, probes, monitor, say=print) -> dict:
    """One partition iteration; the caller runs the dual-signer monitor, this reads its samples for the window.

    The result JSON is written before the final heights are collected, and also for any run that issued
    the disconnect and then failed (outcome reconnect_failed, aborted, or interrupted, with every field
    measured so far); the error is then re-raised. An abort reconnects the container in its cleanup.
    say prints the operator message when the container may still be disconnected.
    """
    started_ms = env.clock.now_ms()
    session.events.append("iteration_start", scenario=SCENARIO, iteration=iteration)
    failures = failover_preconditions(env)
    endpoint, network_failures = primary_endpoint(env)
    require(session, SCENARIO, failures + network_failures, iteration)
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
            _disconnect_and_follow(env, session, iteration, settings, probes, endpoint, progress, result)
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
    result["monitor"] = _monitor_summary(monitor.samples_since(started_ms), result["t_fault_ms"] or started_ms,
                                         env.clock.now_ms())
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


def _monitor_summary(samples: list, fault_ms: int, finish_ms: int) -> dict:
    """The monitor fields of the result; an interval with indeterminate seconds is not observed in full."""
    report = monitor_report(samples, fault_ms, finish_ms)
    observed, problems = report["observed"], list(report["observed_problems"])
    if report["indeterminate_seconds"]:
        observed = False
        problems.append(f"{report['indeterminate_seconds']} indeterminate seconds (a node did not answer /status)")
    return {"observed": observed, "observed_problems": problems,
            "dual_signer_seconds": report["dual_signer_seconds"],
            "indeterminate_seconds": report["indeterminate_seconds"]}


def _remaining_s(env, deadline_ms: int) -> float:
    """Seconds left until deadline_ms, never negative."""
    return max(0.0, (deadline_ms - env.clock.now_ms()) / 1000)


def _disconnect_and_follow(env, session, iteration: int, settings: PartitionSettings, probes, endpoint: dict,
                           progress: dict, result: dict) -> None:
    """Disconnect the primary, follow detection and takeover, reconnect, follow failback, then settle.

    result is filled in as each step finishes, so an abort leaves what was measured. Log markers are
    searched from the instant the disconnect was issued; signatures only from the instant it ended.
    A primary that was never detected is still reconnected.
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
    failback, released, primary_signature = _follow_failback(env, session, iteration, settings, probes, takeover,
                                                             fault_height)
    result.update(failback_complete=failback is not None, failback_source=failback.source if failback else None,
                  primary_first_signature_height=primary_signature.height if primary_signature else None)
    session.events.append("post_recovery_monitoring", iteration=iteration, seconds=settings.post_settle_s)
    env.clock.sleep(settings.post_settle_s)
    restart, restart_source = restart_outcome(probes, fault_ms)
    result.update(restart_outcome=restart, restart_source=restart_source)
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


def _follow_failback(env, session, iteration: int, settings: PartitionSettings, probes, takeover,
                     fault_height: int) -> tuple:
    """(failback, release, primary signature) after the reconnect; each is None when it did not happen.

    Failback only follows a takeover, and each wait runs only when the one before it succeeded.
    """
    if takeover is None:
        return None, None, None
    failback = probes.marker(PRIMARY_NODE, "failback_complete", takeover.t_ms, settings.failback_timeout_s)
    log_observation(session, iteration, "failback_complete", failback)
    if failback is None:
        return None, None, None
    released = probes.marker(BACKUP_NODE, "release_complete", takeover.t_ms, settings.failback_timeout_s)
    log_observation(session, iteration, "release_complete", released)
    if released is None:
        return failback, None, None
    primary_signature = probes.signature(released.t_ms, fault_height, settings.signature_timeout_s)
    log_observation(session, iteration, "primary_first_signature", primary_signature)
    return failback, released, primary_signature


def _outcome(detection, takeover, backup_signature, failback, released, primary_signature) -> str:
    """The first step of the cycle that did not happen, or complete."""
    if detection is None:
        return "not_detected"
    if takeover is None:
        return "no_takeover"
    if failback is None:
        return "no_failback"
    if released is None:
        return "no_release"
    if backup_signature is None or primary_signature is None:
        return "missing_signature"
    return "complete"


def run_partition_series(env, session, count: int, settings: PartitionSettings, probes, monitor, say=print) -> list:
    """Run count iterations with one dual-signer monitor around all of them.

    Any outcome other than complete stops the series, logged as series_aborted; a refusal or an abort
    ends it by raising. Iterations that completed before it stay valid.
    """
    first = next_iteration(session)
    results = []
    monitor.start()
    try:
        for iteration in range(first, first + count):
            result = run_partition(env, session, iteration, settings, probes, monitor, say)
            results.append(result)
            if result["outcome"] != "complete":
                session.events.append("series_aborted", scenario=SCENARIO, iteration=iteration,
                                      outcome=result["outcome"])
                break
    finally:
        monitor.stop()
    return results
