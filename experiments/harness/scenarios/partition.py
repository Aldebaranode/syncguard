"""partition (III-13): cut the primary off from the localnet's Docker network while its process keeps running.

The primary keeps the validator key in memory, so no key holder disappears; it only loses its peers
and the host's published RPC port. The runner records what syncguard and the chain do about it:
detection, takeover, the backup's first signature, and, after the container is reconnected, failback.
The reconnect always happens, in the normal path or in the abort cleanup.
"""

from dataclasses import dataclass

from harness.constants import BACKUP_NODE, PRIMARY_CONTAINER, PRIMARY_NODE, TARGET_VALIDATOR, TIP_MARGIN
from harness.finish import monitor_report
from harness.jsonio import write_json
from harness.metrics import event_time, seconds_between
from harness.preconditions import failover_preconditions
from harness.scenarios.common import (
    abort_recorded,
    best_effort,
    collect_heights_in_chunks,
    container_networks,
    docker_command,
    log_observation,
    next_iteration,
    record_window,
    require,
)

SCENARIO = "partition"
FAULT_KIND = "partition"


@dataclass(frozen=True)
class PartitionSettings:
    """Waits of one partition iteration.

    detection_timeout_s: 60 s for syncguard to detect the unreachable primary.
    takeover_timeout_s: how long the backup has to log its takeover after the disconnect.
    signature_timeout_s: how long to wait for the backup's first signature; the container is
    reconnected once it is seen or this bound passes, whichever comes first.
    failback_timeout_s: how long failback, the release, and the primary's signature each may take.
    post_settle_s: how long the dual-signer monitor keeps running after the reconnect and failback.
    """

    detection_timeout_s: float = 60.0
    takeover_timeout_s: float = 180.0
    signature_timeout_s: float = 180.0
    failback_timeout_s: float = 300.0
    post_settle_s: float = 60.0
    heights_before_fault: int = 30


RESULT_KEYS = (
    "outcome", "network", "t_fault_ms", "fault_height", "fault_exit_code", "command", "detected", "ttd_s",
    "detection_source", "takeover", "takeover_source", "backup_first_signature_height", "ttr_s", "reconnect_ms",
    "reconnect_exit_code", "failback_complete", "failback_source", "primary_first_signature_height",
)


class ReconnectError(Exception):
    """The reconnect command failed; the abort cleanup then retries it."""


def network_argv(verb: str, network: str) -> list:
    """The exact `docker network <verb> <network> validator1-node` command."""
    return ["docker", "network", verb, network, PRIMARY_CONTAINER]


def primary_network(env) -> tuple:
    """(network, failures): the one network the primary is attached to, or the reasons a partition cannot be made.

    The disconnect isolates the node only when it has a single network; with several it would stay reachable.
    """
    networks = container_networks(env, PRIMARY_CONTAINER)
    if networks is None:
        return None, [f"cannot read the networks of {PRIMARY_CONTAINER} from docker inspect"]
    if len(networks) != 1:
        return None, [f"{PRIMARY_CONTAINER} is on {len(networks)} networks ({', '.join(networks)}); "
                      "a partition needs exactly one"]
    return networks[0], []


def reconnect_if_left_disconnected(env, session, iteration: int, network: str, progress: dict) -> None:
    """Abort cleanup: connect the primary back to its network when it was disconnected and is still absent from it.

    Nothing is issued when the disconnect never happened or the reconnect already succeeded. The inspect
    and each connect survive a second interrupt (best effort); the connect is tried twice and
    cleanup_failed is logged with the step if neither attempt succeeded.
    """
    if not progress["needs_reconnect"]:
        session.events.append("abort_cleanup_skipped", scenario=SCENARIO, iteration=iteration, **progress)
        return
    inspected, networks = best_effort(session, SCENARIO, iteration, "inspect",
                                      lambda: container_networks(env, PRIMARY_CONTAINER))
    if inspected and networks is not None and network in networks:
        session.events.append("abort_cleanup_skipped", scenario=SCENARIO, iteration=iteration,
                              container_networks=networks, **progress)
        return
    for attempt in (1, 2):
        done, result = best_effort(session, SCENARIO, iteration, f"reconnect attempt {attempt}", lambda: docker_command(
            env, session, network_argv("connect", network), "network connect", PRIMARY_CONTAINER, SCENARIO,
            iteration, "abort_reconnect", network=network, attempt=attempt))
        if done and result.exit_code == 0:
            progress["needs_reconnect"] = False
            return
    session.events.append("cleanup_failed", scenario=SCENARIO, iteration=iteration, step="reconnect",
                          error="the reconnect did not complete after two attempts")


def run_partition(env, session, iteration: int, settings: PartitionSettings, probes, monitor) -> dict:
    """One partition iteration; the dual-signer monitor runs from before the disconnect until after the settle.

    The result JSON is written before the final heights are collected, so a collection error keeps it.
    An abort reconnects the container in its cleanup and re-raises.
    """
    started_ms = env.clock.now_ms()
    session.events.append("iteration_start", scenario=SCENARIO, iteration=iteration)
    failures = failover_preconditions(env)
    network = None
    if not failures:
        network, failures = primary_network(env)
    require(session, SCENARIO, failures, iteration)
    progress = {"needs_reconnect": False}
    monitor_started_ms = env.clock.now_ms()
    monitor.start()
    try:
        def abort_cleanup() -> None:
            """Reconnect a primary left disconnected, then record the syncguard log window."""
            try:
                reconnect_if_left_disconnected(env, session, iteration, network, progress)
            finally:
                record_window(env, session, iteration, started_ms, env.clock.now_ms())

        with abort_recorded(session, SCENARIO, iteration, abort_cleanup):
            first_height = max(1, env.rpc.tip_height() - settings.heights_before_fault)
            result = _disconnect_and_follow(env, session, iteration, settings, probes, network, progress)
    finally:
        samples = monitor.stop()
    own_samples = [sample for sample in samples if sample.t_ms >= monitor_started_ms]
    report = monitor_report(own_samples, result["t_fault_ms"], env.clock.now_ms())
    result["monitor"] = {key: report[key] for key in
                         ("observed", "observed_problems", "dual_signer_seconds", "indeterminate_seconds")}
    record_window(env, session, iteration, started_ms, env.clock.now_ms())
    result = {"session": session.id, "iteration": iteration, **result}
    write_json(session.directory / f"partition-{iteration}.json", result)
    session.events.append("iteration_result", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
    with abort_recorded(session, SCENARIO, iteration):
        collect_heights_in_chunks(env, session, first_height, env.rpc.tip_height() - TIP_MARGIN)
    session.events.append("iteration_end", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
    return result


def _disconnect_and_follow(env, session, iteration: int, settings: PartitionSettings, probes, network: str,
                           progress: dict) -> dict:
    """Disconnect the primary, follow detection and takeover, reconnect, follow failback, then settle.

    Log markers are searched from the instant the disconnect was issued; signatures only from the
    instant the command ended. A primary that was never detected is still reconnected.
    """
    fault_height = env.rpc.tip_height()
    progress["needs_reconnect"] = True
    disconnect = docker_command(env, session, network_argv("disconnect", network), "network disconnect",
                                PRIMARY_CONTAINER, SCENARIO, iteration, "fault_injected", height=fault_height,
                                fault_kind=FAULT_KIND, network=network)
    result = dict.fromkeys(RESULT_KEYS)
    result.update(network=network, t_fault_ms=disconnect.started_ms, fault_height=fault_height,
                  fault_exit_code=disconnect.exit_code, command=" ".join(disconnect.argv), detected=False,
                  takeover=False, failback_complete=False)
    if disconnect.exit_code != 0:
        networks = container_networks(env, PRIMARY_CONTAINER)
        session.events.append("fault_command_failed", iteration=iteration, exit_code=disconnect.exit_code,
                              container_networks=networks)
        if networks is not None and network in networks:
            progress["needs_reconnect"] = False
            result["outcome"] = "fault_command_failed"
            return result
    fault_ms = disconnect.started_ms
    detection = probes.marker(PRIMARY_NODE, "max_failures", fault_ms, settings.detection_timeout_s)
    log_observation(session, iteration, "max_failures", detection)
    takeover_wait_s = settings.takeover_timeout_s if detection is not None else 0
    takeover = probes.marker(BACKUP_NODE, "takeover_complete", fault_ms, takeover_wait_s)
    if detection is not None:
        log_observation(session, iteration, "takeover_complete", takeover)
    backup_signature = None
    if takeover is not None:
        backup_signature = probes.signature(disconnect.ended_ms, fault_height, settings.signature_timeout_s)
        log_observation(session, iteration, "backup_first_signature", backup_signature)
    reconnect = docker_command(env, session, network_argv("connect", network), "network connect", PRIMARY_CONTAINER,
                               SCENARIO, iteration, "fault_cleared", fault_kind=FAULT_KIND, network=network)
    if reconnect.exit_code != 0:
        raise ReconnectError(f"docker network connect exited {reconnect.exit_code}: {reconnect.stderr.strip()}")
    progress["needs_reconnect"] = False
    failback, released, primary_signature = _follow_failback(env, session, iteration, settings, probes, takeover,
                                                             fault_height)
    session.events.append("post_recovery_monitoring", iteration=iteration, seconds=settings.post_settle_s)
    env.clock.sleep(settings.post_settle_s)
    result.update(
        outcome=_outcome(detection, takeover, backup_signature, failback, released, primary_signature),
        detected=detection is not None,
        ttd_s=seconds_between(fault_ms, event_time(detection)),
        detection_source=detection.source if detection is not None else None,
        takeover=takeover is not None,
        takeover_source=takeover.source if takeover is not None else None,
        backup_first_signature_height=backup_signature.height if backup_signature is not None else None,
        ttr_s=seconds_between(fault_ms, backup_signature.signers[TARGET_VALIDATOR]) if backup_signature is not None else None,
        reconnect_ms=reconnect.started_ms,
        reconnect_exit_code=reconnect.exit_code,
        failback_complete=failback is not None,
        failback_source=failback.source if failback is not None else None,
        primary_first_signature_height=primary_signature.height if primary_signature is not None else None,
    )
    return result


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


def run_partition_series(env, session, count: int, settings: PartitionSettings, probes, monitor) -> list:
    """Run count iterations; any outcome other than complete stops the series, logged as series_aborted.

    A refusal or an abort ends the series by raising. Iterations that completed before it stay valid.
    """
    first = next_iteration(session)
    results = []
    for iteration in range(first, first + count):
        result = run_partition(env, session, iteration, settings, probes, monitor)
        results.append(result)
        if result["outcome"] != "complete":
            session.events.append("series_aborted", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
            break
    return results
