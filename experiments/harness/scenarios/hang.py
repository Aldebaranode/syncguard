"""hang (R9, III-8): docker pause the primary, record detection, the restart attempt, and a safe end of the hang.

A paused process keeps the validator key in memory. Unpausing it is safe only
when it is provably the sole holder of the key (see clearing_action); every
other case, and any doubt, ends the hang with docker kill. The decision, its
inputs, and the clearing command's result are logged and stored. After a
handover or a clearing kill the runner follows the failover and the failback
that restore the standard layout, then settles, so the next run's preconditions
pass. The runner stores instants only; the analysis computes TTD and TTR.
"""

from dataclasses import dataclass

from harness.adapters import Unreachable
from harness.constants import BACKUP_NODE, PRIMARY_CONTAINER, PRIMARY_NODE, TARGET_VALIDATOR, TIP_MARGIN
from harness.jsonio import write_json
from harness.metrics import event_time
from harness.monitor import UNREACHABLE
from harness.preconditions import failover_preconditions
from harness.scenarios.common import (
    KILLED_STATES,
    abort_recorded,
    best_effort,
    collect_heights_in_chunks,
    container_status,
    cycle_outcome,
    docker_action,
    follow_failback,
    loaded_address_or_unreachable,
    log_observation,
    record_window,
    require,
    run_series,
    two_attempts,
)
from harness.signing import parse_commit, shift_to_host_clock

SCENARIO = "hang"
TERMINAL_POLL_S = 1.0


@dataclass(frozen=True)
class HangSettings:
    """Waits of one hang iteration.

    detection_timeout_s: 60 s for detection (analytic bound 35 s).
    stop_timeout_s: syncguard's stop_timeout (50 s in the evaluation config); the terminal
    outcome of an aborted failover is awaited for 2 x stop_timeout_s + terminal_margin_s from detection.
    takeover_timeout_s, failback_timeout_s, signature_timeout_s: the follow-up waits after a handover or
    a clearing kill (failover completion and takeover; failback and release; each first signature).
    post_clear_settle_s: how long the dual-signer monitor keeps running after the follow-up; at least the
    90 s role-change spacing, so the next run's preconditions pass.
    """

    detection_timeout_s: float = 60.0
    stop_timeout_s: float = 50.0
    terminal_margin_s: float = 30.0
    takeover_timeout_s: float = 180.0
    failback_timeout_s: float = 300.0
    signature_timeout_s: float = 180.0
    post_clear_settle_s: float = 100.0
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


def handover_event(probes, since_ms: int):
    """The first log event showing the key was handed over (either node's log) since since_ms, or None."""
    return (probes.marker(PRIMARY_NODE, "failover_complete", since_ms, 0)
            or probes.marker(BACKUP_NODE, "failover_notified", since_ms, 0)
            or probes.marker(BACKUP_NODE, "takeover_started", since_ms, 0))


def wait_terminal_outcome(env, probes, since_ms: int, bound_s: float) -> tuple:
    """Poll for the outcome of the failover the hang triggered: handed_over, aborted, or None at the bound."""
    deadline = env.clock.now_ms() + bound_s * 1000
    while True:
        handover = handover_event(probes, since_ms)
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

    Heights are read through the tip: a flag-2 entry in a commit that is not final yet is still a signature.
    checked is False when the commits could not be read; the caller treats that as doubt.
    """
    try:
        last = env.rpc.tip_height()
        records = [parse_commit(env.rpc.commit(height)) for height in range(first_height, last + 1)]
        records = shift_to_host_clock(records, session.offset_ms)
    except Unreachable:
        return False, None
    for record in records:
        if record.header_time_ms > since_ms and TARGET_VALIDATOR in record.signers:
            return True, record.height
    return True, None


RESULT_KEYS = (
    "outcome", "detected", "t_fault_ms", "t_fault_ended_ms", "t_detect_ms", "detection_source", "restart_outcome",
    "restart_source", "pause_exit_code", "fault_injected", "clear_exit_code", "followup_kill_exit_code",
    "state_after", "clear_ok", "container_state", "terminal_outcome", "terminal_source", "signer_check_ok",
    "other_signer_height", "backup_address", "late_handover_source", "action", "reason", "followup",
    "t_failover_complete_ms", "t_takeover_ms", "backup_signature_height", "t_backup_signature_ms",
    "t_failback_complete_ms", "t_release_complete_ms", "primary_signature_height", "t_primary_signature_ms",
    "followup_sources",
)
NO_FOLLOWUP = "not_needed"


def kill_if_left_behind(env, session, iteration: int, progress: dict) -> None:
    """Abort cleanup: after a pause was issued and not yet cleared, kill the primary unless it is exited or dead.

    The decision and the command result are logged. The abort path never unpauses, because after an
    error nothing has verified that the paused process is the only key holder. The inspect and the
    kill survive a second interrupt (best effort); the kill is retried once, and cleanup_failed is
    logged with the error if it still did not complete.
    """
    if not progress["pause_issued"] or progress["cleared"]:
        session.events.append("abort_cleanup_skipped", scenario=SCENARIO, iteration=iteration, **progress)
        return
    inspected, state = best_effort(session, SCENARIO, iteration, "inspect", lambda: container_status(env, PRIMARY_CONTAINER))
    state = state if inspected else "unknown"
    if state in KILLED_STATES:
        session.events.append("abort_cleanup_skipped", scenario=SCENARIO, iteration=iteration,
                              container_state=state, **progress)
        return
    two_attempts(session, SCENARIO, iteration, "kill", lambda attempt: docker_action(
        env, session, "kill", PRIMARY_CONTAINER, SCENARIO, iteration, "abort_kill", container_state=state,
        attempt=attempt))
    _, state_after = best_effort(session, SCENARIO, iteration, "inspect after kill",
                                 lambda: container_status(env, PRIMARY_CONTAINER))
    session.events.append("abort_kill_result", scenario=SCENARIO, iteration=iteration, state_after=state_after)


def run_hang(env, session, iteration: int, settings: HangSettings, probes) -> dict:
    """One hang iteration; the series around it runs the dual-signer monitor until after the settle period.

    The result JSON is written before the final heights are collected, so a collection error keeps it.
    """
    started_ms = env.clock.now_ms()
    require(session, SCENARIO, failover_preconditions(env), iteration)
    session.events.append("iteration_start", scenario=SCENARIO, iteration=iteration)
    first_height = max(1, env.rpc.tip_height() - settings.heights_before_fault)
    progress = {"pause_issued": False, "cleared": False}

    def abort_cleanup() -> None:
        """Kill a paused container left behind, then record the syncguard log window."""
        try:
            kill_if_left_behind(env, session, iteration, progress)
        finally:
            record_window(env, session, iteration, started_ms, env.clock.now_ms())

    with abort_recorded(session, SCENARIO, iteration, abort_cleanup):
        result = _pause_and_clear(env, session, iteration, settings, probes, progress)
    record_window(env, session, iteration, started_ms, env.clock.now_ms())
    write_json(session.directory / f"hang-{iteration}.json", {"session": session.id, "iteration": iteration, **result})
    with abort_recorded(session, SCENARIO, iteration):
        collect_heights_in_chunks(env, session, first_height, env.rpc.tip_height() - TIP_MARGIN)
    session.events.append("iteration_end", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
    return result


def run_hang_series(env, session, count: int, settings: HangSettings, probes, monitor) -> list:
    """Run count hang iterations with one dual-signer monitor around all of them (common.run_series).

    Any outcome other than complete stops the series, logged as series_aborted; a refusal or an abort
    ends it by raising. Iterations that completed before it stay valid.
    """

    def run_one(iteration: int) -> dict:
        """One hang run."""
        return run_hang(env, session, iteration, settings, probes)

    return run_series(session, SCENARIO, count, monitor, run_one, lambda result: result["outcome"])


def _not_injected(pause, state: str) -> dict:
    """Result of a hang whose pause command failed and left the container in a state that needs no clearing.

    Every key of the full result is present; the ones that never happened are None.
    """
    result = dict.fromkeys(RESULT_KEYS)
    result.update({"outcome": "fault_command_failed", "detected": False, "restart_outcome": "not_attempted",
                   "container_state": state, "pause_exit_code": pause.exit_code, "fault_injected": False,
                   "t_fault_ms": pause.started_ms, "t_fault_ended_ms": pause.ended_ms, "action": "none",
                   "followup": NO_FOLLOWUP, "followup_sources": {},
                   "reason": pause.stderr.strip() or f"pause exited {pause.exit_code}; container is {state}"})
    return result


def _pause_and_clear(env, session, iteration: int, settings: HangSettings, probes, progress: dict) -> dict:
    """Pause the primary, observe detection and the failover outcome, end the hang by the clearing rule, settle."""
    fault_height = env.rpc.tip_height()
    progress["pause_issued"] = True
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
    late_handover_source = None
    if action == "unpause":
        late = handover_event(probes, pause.started_ms)
        if late is not None:
            late_handover_source = late.source
            action, reason = "kill", f"a handover marker appeared before the unpause ({late.source})"
    decision = {"container_state": state, "terminal_outcome": terminal, "terminal_source": terminal_source,
                "signer_check_ok": signer_check_ok, "other_signer_height": other_signer,
                "backup_address": backup_address, "late_handover_source": late_handover_source,
                "action": action, "reason": reason}
    session.events.append("hang_decision", iteration=iteration, **decision)
    clear_exit_code, state_after = None, state
    if action != "none":
        cleared = docker_action(env, session, action, PRIMARY_CONTAINER, SCENARIO, iteration, "fault_cleared")
        clear_exit_code = cleared.exit_code
        state_after = container_status(env, PRIMARY_CONTAINER)
    followup_kill_exit_code = None
    effective_action = action
    if action == "unpause" and state_after == "paused":
        followup = docker_action(env, session, "kill", PRIMARY_CONTAINER, SCENARIO, iteration,
                                 "fault_cleared_after_failed_unpause")
        followup_kill_exit_code = followup.exit_code
        state_after = container_status(env, PRIMARY_CONTAINER)
        effective_action = "kill"
    clear_ok = _cleared(effective_action, state_after)
    progress["cleared"] = True
    session.events.append("hang_cleared", iteration=iteration, action=action, exit_code=clear_exit_code,
                          state_after=state_after, clear_ok=clear_ok)
    if terminal == "aborted" and effective_action != "kill":
        recovery = {"followup": NO_FOLLOWUP, "followup_sources": {}}
    else:
        recovery = _follow_recovery(session, iteration, settings, probes, pause, fault_height)
    session.events.append("post_clear_settle", iteration=iteration, seconds=settings.post_clear_settle_s)
    env.clock.sleep(settings.post_clear_settle_s)
    result = dict.fromkeys(RESULT_KEYS)
    result.update({
        "detected": detection is not None,
        "t_fault_ms": pause.started_ms,
        "t_fault_ended_ms": pause.ended_ms,
        "t_detect_ms": event_time(detection),
        "detection_source": detection.source if detection is not None else None,
        "restart_outcome": restart,
        "restart_source": restart_source,
        "pause_exit_code": pause.exit_code,
        "fault_injected": True,
        "clear_exit_code": clear_exit_code,
        "followup_kill_exit_code": followup_kill_exit_code,
        "state_after": state_after,
        "clear_ok": clear_ok,
        **decision,
        **recovery,
    })
    result["outcome"] = _outcome(result)
    return result


def _outcome(result: dict) -> str:
    """The hang's outcome: the first thing that went wrong, or complete.

    A failed clear comes first, then a hang that was never detected, then a follow-up that did not
    restore the layout; complete means detected, ended safely, and back in the standard layout.
    """
    if not result["clear_ok"]:
        return "clear_failed"
    if not result["detected"]:
        return "not_detected"
    if result["followup"] not in (NO_FOLLOWUP, "complete"):
        return result["followup"]
    return "complete"


def _follow_recovery(session, iteration: int, settings: HangSettings, probes, pause, fault_height: int) -> dict:
    """After a handover or a clearing kill: follow the failover and failback that restore the standard layout.

    Waits, each bounded and logged, for "Failover complete" on the primary and the backup's takeover (from
    the pause), the backup's first signature (cast after the pause command ended), then failback, the
    release, and the primary's first signature. Returns the follow-up outcome and every instant found.
    """
    failover = probes.marker(PRIMARY_NODE, "failover_complete", pause.started_ms, settings.takeover_timeout_s)
    log_observation(session, iteration, "failover_complete", failover)
    takeover, backup_signature = None, None
    if failover is not None:
        takeover = probes.marker(BACKUP_NODE, "takeover_complete", pause.started_ms, settings.takeover_timeout_s)
        log_observation(session, iteration, "takeover_complete", takeover)
    if takeover is not None:
        backup_signature = probes.signature(pause.ended_ms, fault_height, settings.signature_timeout_s)
        log_observation(session, iteration, "backup_first_signature", backup_signature)
    failback, released, primary_signature = follow_failback(session, iteration, probes, takeover, fault_height,
                                                            settings.failback_timeout_s, settings.signature_timeout_s)
    followup = "no_failover" if failover is None else cycle_outcome(takeover, backup_signature, failback, released,
                                                                     primary_signature)
    named = {"failover_complete": failover, "takeover": takeover, "failback_complete": failback,
             "release_complete": released}
    return {
        "followup": followup,
        "t_failover_complete_ms": event_time(failover),
        "t_takeover_ms": event_time(takeover),
        "backup_signature_height": backup_signature.height if backup_signature is not None else None,
        "t_backup_signature_ms": backup_signature.signers[TARGET_VALIDATOR] if backup_signature is not None else None,
        "t_failback_complete_ms": event_time(failback),
        "t_release_complete_ms": event_time(released),
        "primary_signature_height": primary_signature.height if primary_signature is not None else None,
        "t_primary_signature_ms": (primary_signature.signers[TARGET_VALIDATOR]
                                   if primary_signature is not None else None),
        "followup_sources": {name: event.source for name, event in named.items() if event is not None},
    }


def _cleared(action: str, state_after: str) -> bool:
    """Whether the container ended in the state the clearing action should leave it in."""
    if action == "kill":
        return state_after in KILLED_STATES
    if action == "unpause":
        return state_after == "running"
    return state_after != "paused"
