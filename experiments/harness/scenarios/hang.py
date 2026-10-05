"""hang (R9, III-8): docker pause the primary, record detection, the restart attempt, and a safe end of the hang.

A paused process keeps the validator key in memory. Unpausing it is safe only
when it would be the sole holder of the key; otherwise the hang ends with
docker kill, and the decision with its reason is logged.
"""

from dataclasses import dataclass

from harness.constants import PRIMARY_CONTAINER, PRIMARY_NODE, TARGET_VALIDATOR, TIP_MARGIN
from harness.jsonio import write_json
from harness.metrics import event_time, seconds_between
from harness.preconditions import failover_preconditions
from harness.scenarios.common import (
    container_status,
    docker_action,
    loaded_address_or_unreachable,
    log_observation,
    record_heights,
    record_window,
    require,
)

SCENARIO = "hang"


@dataclass(frozen=True)
class HangSettings:
    """Waits of one hang iteration: 60 s for detection (bound 45 s), 90 s for the restart attempt (stop_timeout 50 s)."""

    detection_timeout_s: float = 60.0
    settle_s: float = 90.0
    heights_before_fault: int = 30


def clearing_action(container_state: str, stop_failed: bool, backup_address: str, validator: str) -> tuple:
    """How to end the hang: none when the paused process is gone, kill when unpausing could add a signer, else unpause."""
    if container_state != "paused":
        return "none", f"container is {container_state}; the paused process no longer exists"
    if stop_failed:
        return "kill", "syncguard reported it could not stop the node"
    if backup_address == validator:
        return "kill", "the backup runs the validator key; unpausing would add a second signer"
    return "unpause", "the paused process is the only holder of the validator key"


def restart_outcome(probes, since_ms: int) -> tuple:
    """How syncguard's restart of the paused container ended, read from its log."""
    restarted = probes.marker(PRIMARY_NODE, "container_restarted", since_ms, 0)
    if restarted is not None:
        return "succeeded", restarted.source
    failed = probes.marker(PRIMARY_NODE, "restart_failed", since_ms, 0)
    if failed is not None:
        return "failed", failed.source
    return "not_attempted", None


def run_hang(env, session, iteration: int, settings: HangSettings, probes, monitor) -> dict:
    """One hang iteration with the dual-signer monitor running throughout."""
    started_ms = env.clock.now_ms()
    session.events.append("iteration_start", scenario=SCENARIO, iteration=iteration)
    require(session, SCENARIO, failover_preconditions(env), iteration)
    first_height = max(1, env.rpc.tip_height() - settings.heights_before_fault)
    monitor.start()
    try:
        result = _pause_and_clear(env, session, iteration, settings, probes)
    finally:
        monitor.stop()
    record_window(env, session, iteration, started_ms, env.clock.now_ms())
    record_heights(env, session, first_height, env.rpc.tip_height() - TIP_MARGIN)
    write_json(session.directory / f"hang-{iteration}.json", {"session": session.id, "iteration": iteration, **result})
    session.events.append("iteration_end", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
    return result


def _pause_and_clear(env, session, iteration: int, settings: HangSettings, probes) -> dict:
    """Pause the primary, observe detection and the restart attempt, then end the hang by the clearing rule."""
    fault_height = env.rpc.tip_height()
    pause = docker_action(env, session, "pause", PRIMARY_CONTAINER, SCENARIO, iteration, "fault_injected",
                          height=fault_height)
    if pause.exit_code != 0:
        return {"outcome": "fault_command_failed", "detected": False, "ttd_s": None, "detection_source": None,
                "restart_outcome": "not_attempted", "restart_source": None, "container_state": "unknown",
                "action": "none", "reason": pause.stderr.strip()}
    detection = probes.marker(PRIMARY_NODE, "max_failures", pause.started_ms, settings.detection_timeout_s)
    log_observation(session, iteration, "max_failures", detection)
    if detection is not None:
        env.clock.sleep(settings.settle_s)
    restart, restart_source = restart_outcome(probes, pause.started_ms)
    stop_failed = probes.marker(PRIMARY_NODE, "failover_aborted", pause.started_ms, 0) is not None
    state = container_status(env, PRIMARY_CONTAINER)
    backup_address = loaded_address_or_unreachable(env.backup_rpc)
    action, reason = clearing_action(state, stop_failed, backup_address, TARGET_VALIDATOR)
    session.events.append("hang_decision", iteration=iteration, container_state=state, stop_failed=stop_failed,
                          backup_address=backup_address, action=action, reason=reason)
    if action != "none":
        docker_action(env, session, action, PRIMARY_CONTAINER, SCENARIO, iteration, "fault_cleared")
    return {
        "outcome": "detected" if detection is not None else "not_detected",
        "detected": detection is not None,
        "ttd_s": seconds_between(pause.started_ms, event_time(detection)),
        "detection_source": detection.source if detection is not None else None,
        "restart_outcome": restart,
        "restart_source": restart_source,
        "container_state": state,
        "action": action,
        "reason": reason,
    }
