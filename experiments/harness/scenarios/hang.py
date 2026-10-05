"""hang (R9, III-8): docker pause the primary, record detection, the restart attempt, and a safe end of the hang.

A paused process keeps the validator key in memory. Unpausing it is safe only
when it is provably the sole holder of the key (see clearing_action); every
other case, and any doubt, ends the hang with docker kill. The decision, its
inputs, and the clearing command's result are logged and stored.
"""

from dataclasses import dataclass

from harness.adapters import Unreachable
from harness.constants import BACKUP_NODE, PRIMARY_CONTAINER, PRIMARY_NODE, TARGET_VALIDATOR, TIP_MARGIN
from harness.jsonio import write_json
from harness.metrics import event_time, seconds_between
from harness.monitor import UNREACHABLE
from harness.preconditions import failover_preconditions
from harness.scenarios.common import (
    abort_recorded,
    container_status,
    docker_action,
    loaded_address_or_unreachable,
    log_observation,
    record_heights,
    record_window,
    require,
)
from harness.signing import collect_heights, shift_to_host_clock

SCENARIO = "hang"
TERMINAL_POLL_S = 1.0
KILLED_STATES = ("exited", "dead")


@dataclass(frozen=True)
class HangSettings:
    """Waits of one hang iteration.

    detection_timeout_s: 60 s for detection (bound 45 s).
    stop_timeout_s: syncguard's stop_timeout (50 s in the evaluation config); the terminal
    outcome of an aborted failover is awaited for 2 x stop_timeout_s + terminal_margin_s from detection.
    post_clear_settle_s: how long the dual-signer monitor keeps running after the clearing action.
    """

    detection_timeout_s: float = 60.0
    stop_timeout_s: float = 50.0
    terminal_margin_s: float = 30.0
    post_clear_settle_s: float = 60.0
    heights_before_fault: int = 30

    @property
    def terminal_wait_s(self) -> float:
        """Upper bound for syncguard to log a terminal outcome after detecting the hang."""
        return 2 * self.stop_timeout_s + self.terminal_margin_s


def clearing_action(container_state: str, terminal_outcome: str | None, other_signer_height: int | None,
                    signer_check_ok: bool, backup_address: str, validator: str) -> tuple:
    """How to end the hang; unpause only when every safety condition holds, otherwise kill.

    Unpause requires: the container is paused, syncguard logged an aborted failover (no handover),
    the signer check ran and found no signature by the validator after the pause began, and the
    backup answered /status with a non-validator address. Any doubt (unknown state, no terminal
    outcome, unreachable backup or rpc) means kill.
    """
    if container_state == "unknown":
        return "kill", "container state is unknown; killing is the only safe end"
    if container_state != "paused":
        return "none", f"container is {container_state}; the paused process no longer exists"
    if terminal_outcome == "handed_over":
        return "kill", "syncguard handed the key over; unpausing would add a second signer"
    if terminal_outcome != "aborted":
        return "kill", "syncguard logged no terminal outcome for the failover within the wait bound"
    if not signer_check_ok:
        return "kill", "the commits after the pause could not be read, so another signer cannot be ruled out"
    if other_signer_height is not None:
        return "kill", f"the validator signed height {other_signer_height} after the pause; another process holds the key"
    if backup_address == UNREACHABLE:
        return "kill", "the backup's /status is unreachable, so its key cannot be confirmed"
    if backup_address == validator:
        return "kill", "the backup runs the validator key; unpausing would add a second signer"
    return "unpause", "failover aborted without handover, no other signer seen, and the backup holds a mock key"


def restart_outcome(probes, since_ms: int) -> tuple:
    """How syncguard's restart of the paused container ended; both markers are reported in log order.

    succeeded, failed, failed_then_succeeded, succeeded_then_failed, or not_attempted;
    the source joins the file:line of every marker found.
    """
    restarted = probes.marker(PRIMARY_NODE, "container_restarted", since_ms, 0)
    failed = probes.marker(PRIMARY_NODE, "restart_failed", since_ms, 0)
    if restarted is None and failed is None:
        return "not_attempted", None
    if failed is None:
        return "succeeded", restarted.source
    if restarted is None:
        return "failed", failed.source
    if failed.t_ms <= restarted.t_ms:
        return "failed_then_succeeded", f"{failed.source}; {restarted.source}"
    return "succeeded_then_failed", f"{restarted.source}; {failed.source}"


def wait_terminal_outcome(env, probes, since_ms: int, bound_s: float) -> tuple:
    """Poll for the outcome of the failover the hang triggered: handed_over, aborted, or None at the bound."""
    deadline = env.clock.now_ms() + bound_s * 1000
    while True:
        handover = (probes.marker(PRIMARY_NODE, "failover_complete", since_ms, 0)
                    or probes.marker(BACKUP_NODE, "failover_notified", since_ms, 0)
                    or probes.marker(BACKUP_NODE, "takeover_started", since_ms, 0))
        if handover is not None:
            return "handed_over", handover.source
        aborted = probes.marker(PRIMARY_NODE, "failover_aborted", since_ms, 0)
        if aborted is not None:
            return "aborted", aborted.source
        if env.clock.now_ms() >= deadline:
            return None, None
        env.clock.sleep(TERMINAL_POLL_S)


def other_signer_after(env, session, first_height: int, since_ms: int) -> tuple:
    """(checked, height): the first height whose header time is after since_ms and that the validator signed.

    checked is False when the commits could not be read; the caller treats that as doubt.
    """
    try:
        last = env.rpc.tip_height() - TIP_MARGIN
        records = shift_to_host_clock(collect_heights(env.rpc, first_height, last), session.offset_ms) \
            if last >= first_height else []
    except Unreachable:
        return False, None
    for record in records:
        if record.header_time_ms > since_ms and TARGET_VALIDATOR in record.signers:
            return True, record.height
    return True, None


def run_hang(env, session, iteration: int, settings: HangSettings, probes, monitor) -> dict:
    """One hang iteration; the dual-signer monitor runs from before the pause until after the settle period."""
    started_ms = env.clock.now_ms()
    session.events.append("iteration_start", scenario=SCENARIO, iteration=iteration)
    require(session, SCENARIO, failover_preconditions(env), iteration)
    first_height = max(1, env.rpc.tip_height() - settings.heights_before_fault)
    monitor.start()
    try:
        def record_log_window() -> None:
            """Record the syncguard log window up to now for an aborted iteration."""
            record_window(env, session, iteration, started_ms, env.clock.now_ms())

        with abort_recorded(session, SCENARIO, iteration, record_log_window):
            result = _pause_and_clear(env, session, iteration, settings, probes)
    finally:
        monitor.stop()
    record_window(env, session, iteration, started_ms, env.clock.now_ms())
    record_heights(env, session, first_height, env.rpc.tip_height() - TIP_MARGIN)
    write_json(session.directory / f"hang-{iteration}.json", {"session": session.id, "iteration": iteration, **result})
    session.events.append("iteration_end", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
    return result


def _not_injected(pause, state: str) -> dict:
    """Result of a hang whose pause command failed and left the container in a state that needs no clearing."""
    return {"outcome": "fault_command_failed", "detected": False, "ttd_s": None, "detection_source": None,
            "restart_outcome": "not_attempted", "restart_source": None, "container_state": state,
            "pause_exit_code": pause.exit_code, "fault_injected": False, "action": "none",
            "reason": pause.stderr.strip() or f"pause exited {pause.exit_code}; container is {state}"}


def _pause_and_clear(env, session, iteration: int, settings: HangSettings, probes) -> dict:
    """Pause the primary, observe detection and the failover outcome, end the hang by the clearing rule, settle."""
    fault_height = env.rpc.tip_height()
    pause = docker_action(env, session, "pause", PRIMARY_CONTAINER, SCENARIO, iteration, "fault_injected",
                          height=fault_height)
    detection = None
    terminal, terminal_source = None, None
    pause_took_effect = pause.exit_code == 0
    if not pause_took_effect:
        state_after_pause = container_status(env, PRIMARY_CONTAINER)
        session.events.append("pause_command_failed", iteration=iteration, exit_code=pause.exit_code,
                              container_state=state_after_pause)
        if state_after_pause not in ("paused", "unknown"):
            return _not_injected(pause, state_after_pause)
        pause_took_effect = state_after_pause == "paused"
    if pause_took_effect:
        detection = probes.marker(PRIMARY_NODE, "max_failures", pause.started_ms, settings.detection_timeout_s)
        log_observation(session, iteration, "max_failures", detection)
        if detection is not None:
            terminal, terminal_source = wait_terminal_outcome(env, probes, pause.started_ms, settings.terminal_wait_s)
    restart, restart_source = restart_outcome(probes, pause.started_ms)
    state = container_status(env, PRIMARY_CONTAINER)
    signer_check_ok, other_signer = other_signer_after(env, session, fault_height, pause.started_ms)
    backup_address = loaded_address_or_unreachable(env.backup_rpc)
    action, reason = clearing_action(state, terminal, other_signer, signer_check_ok, backup_address, TARGET_VALIDATOR)
    decision = {"container_state": state, "terminal_outcome": terminal, "terminal_source": terminal_source,
                "signer_check_ok": signer_check_ok, "other_signer_height": other_signer,
                "backup_address": backup_address, "action": action, "reason": reason}
    session.events.append("hang_decision", iteration=iteration, **decision)
    clear_exit_code, state_after = None, state
    if action != "none":
        cleared = docker_action(env, session, action, PRIMARY_CONTAINER, SCENARIO, iteration, "fault_cleared")
        clear_exit_code = cleared.exit_code
        state_after = container_status(env, PRIMARY_CONTAINER)
    clear_ok = _cleared(action, state_after)
    session.events.append("hang_cleared", iteration=iteration, action=action, exit_code=clear_exit_code,
                          state_after=state_after, clear_ok=clear_ok)
    session.events.append("post_clear_settle", iteration=iteration, seconds=settings.post_clear_settle_s)
    env.clock.sleep(settings.post_clear_settle_s)
    outcome = "clear_failed" if not clear_ok else ("detected" if detection is not None else "not_detected")
    return {
        "outcome": outcome,
        "detected": detection is not None,
        "ttd_s": seconds_between(pause.started_ms, event_time(detection)),
        "detection_source": detection.source if detection is not None else None,
        "restart_outcome": restart,
        "restart_source": restart_source,
        "pause_exit_code": pause.exit_code,
        "fault_injected": True,
        "clear_exit_code": clear_exit_code,
        "state_after": state_after,
        "clear_ok": clear_ok,
        **decision,
    }


def _cleared(action: str, state_after: str) -> bool:
    """Whether the container ended in the state the clearing action should leave it in."""
    if action == "kill":
        return state_after in KILLED_STATES
    if action == "unpause":
        return state_after == "running"
    return state_after != "paused"
