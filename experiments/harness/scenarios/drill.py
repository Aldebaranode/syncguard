"""baseline_drill (R9, D3): operator-in-the-loop manual recovery without syncguard.

The alert instant is when the harness sees the first committed height without
a 48DC2183 signature after the kill, the trigger a missed-block alert uses; when
no such height is seen the alert is recorded as not observed.
Recovery is measured from the instant the kill command was issued (t_fault_ms)
to the first 48DC2183 signature cast after the kill command ended and after the
header time of the alert height, so a precommit cast before the kill landed
cannot count. The dual-signer monitor runs from before the kill until the
operator has restored the layout (types RESTORED_WORD) plus the post-restore period.
A kill that failed, or left the container running, aborts the drill.
"""

from dataclasses import dataclass

from harness.constants import PRIMARY_CONTAINER, TARGET_VALIDATOR, TIP_MARGIN
from harness.jsonio import write_json
from harness.metrics import seconds_between
from harness.preconditions import check_loaded_addresses, check_syncguard_stopped, collect_failures, drill_preconditions
from harness.scenarios.common import (
    KILLED_STATES,
    abort_recorded,
    collect_heights_in_chunks,
    container_status,
    docker_action,
    log_observation,
    next_iteration,
    record_window,
    require,
)

SCENARIO = "baseline_drill"
READY_WORD = "ready"
DONE_WORD = "done"
RESTORED_WORD = "restored"


@dataclass(frozen=True)
class DrillSettings:
    """The operator's name and the waits of the drill."""

    operator: str
    alert_timeout_s: float = 120.0
    signature_timeout_s: float = 900.0
    heights_before_fault: int = 30
    post_restore_monitor_s: float = 30.0


def _operator_commands(env, session, iteration: int, ask, say) -> list:
    """Run each command the operator types until DONE_WORD, logging it with host timestamps."""
    commands = []
    while True:
        line = ask(f"command (or '{DONE_WORD}')> ").strip()
        if line == DONE_WORD:
            return commands
        if not line:
            continue
        result = env.commands.run(["/bin/sh", "-c", line], timeout=600)
        session.events.append("operator_command", t_ms=result.started_ms, iteration=iteration, command=line,
                              exit_code=result.exit_code, ended_ms=result.ended_ms)
        say(result.stdout + result.stderr)
        commands.append({"command": line, "exit_code": result.exit_code,
                         "started_ms": result.started_ms, "ended_ms": result.ended_ms})


def run_drill(env, session, settings: DrillSettings, probes, monitor, ask, say) -> dict:
    """Kill the primary, raise the alert, record the operator's commands, and wait for the first signature.

    The drill refuses unless a named operator types READY_WORD; syncguard-stopped and loaded-key checks run
    again after that confirmation, because they can change while the operator reads the runbook.
    """
    iteration = next_iteration(session)
    started_ms = env.clock.now_ms()
    if not settings.operator.strip():
        require(session, SCENARIO, ["a human operator is required; pass --operator with the operator's name"], iteration)
    require(session, SCENARIO, drill_preconditions(env), iteration)
    say(f"Operator {settings.operator}: open {env.paths.runbook} in a second window.")
    if ask(f"Type '{READY_WORD}' to kill {PRIMARY_CONTAINER} and start the clock: ").strip() != READY_WORD:
        require(session, SCENARIO, ["the operator did not confirm the start"], iteration)
    require(session, SCENARIO, collect_failures([
        ("syncguard stopped", lambda: check_syncguard_stopped(env)),
        ("loaded keys", lambda: check_loaded_addresses(env, TARGET_VALIDATOR)),
    ]), iteration)
    session.events.append("iteration_start", scenario=SCENARIO, iteration=iteration)
    first_height = max(1, env.rpc.tip_height() - settings.heights_before_fault)
    fault_height = env.rpc.tip_height()
    result = {"operator": settings.operator, "iteration": iteration, "outcome": "aborted"}
    monitor.start()

    def record_log_window() -> None:
        """Record the syncguard log window up to now for an aborted drill."""
        record_window(env, session, iteration, started_ms, env.clock.now_ms())

    try:
        with abort_recorded(session, SCENARIO, iteration, record_log_window):
            fault = docker_action(env, session, "kill", PRIMARY_CONTAINER, SCENARIO, iteration, "fault_injected",
                                  height=fault_height)
            result.update(t_fault_ms=fault.started_ms, t_fault_ended_ms=fault.ended_ms)
            state = container_status(env, PRIMARY_CONTAINER)
            if fault.exit_code != 0 or state not in KILLED_STATES:
                session.events.append("drill_kill_failed", iteration=iteration, exit_code=fault.exit_code,
                                      container_state=state)
                result.update(_kill_failed(settings, iteration, fault, state))
                say(f"The kill of {PRIMARY_CONTAINER} failed (exit {fault.exit_code}); container state {state}. "
                    f"The drill was aborted without measurement. If {PRIMARY_CONTAINER} is down, follow the "
                    f"'After the drill' section of {env.paths.runbook} to restore the layout.")
            else:
                result.update(_after_kill(env, session, iteration, settings, probes, ask, say, fault, fault_height))
                result["restore_completed"] = False
                write_json(session.directory / "drill.json", {"session": session.id, **result})
                result.update(_restore(env, session, iteration, settings, ask, say))
                result["restore_completed"] = True
    finally:
        monitor.stop()
        write_json(session.directory / "drill.json", {"session": session.id, **result})
    with abort_recorded(session, SCENARIO, iteration):
        collect_heights_in_chunks(env, session, first_height, env.rpc.tip_height() - TIP_MARGIN)
    session.events.append("iteration_end", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
    return {"session": session.id, **result}


def _kill_failed(settings: DrillSettings, iteration: int, fault, state: str) -> dict:
    """drill.json fields of a drill that never started because the kill did not stop the primary."""
    return {"operator": settings.operator, "iteration": iteration, "outcome": "fault_command_failed",
            "t_fault_ms": fault.started_ms, "t_fault_ended_ms": fault.ended_ms, "kill_exit_code": fault.exit_code,
            "container_state": state, "recovery_s": None, "first_signature_height": None}


def _restore(env, session, iteration: int, settings: DrillSettings, ask, say) -> dict:
    """Ask the operator to restore the layout per the runbook; the monitor keeps running until then plus 30 s."""
    say(f"Restore the layout now, following the 'After the drill' section of {env.paths.runbook}.")
    restore_start_ms = env.clock.now_ms()
    session.events.append("restore_start", t_ms=restore_start_ms, iteration=iteration)
    while ask(f"Type '{RESTORED_WORD}' when the layout is restored: ").strip() != RESTORED_WORD:
        say(f"Waiting for '{RESTORED_WORD}'.")
    restore_end_ms = env.clock.now_ms()
    session.events.append("restore_end", t_ms=restore_end_ms, iteration=iteration)
    session.events.append("post_restore_monitoring", iteration=iteration, seconds=settings.post_restore_monitor_s)
    env.clock.sleep(settings.post_restore_monitor_s)
    return {"t_restore_start_ms": restore_start_ms, "t_restore_end_ms": restore_end_ms}


def _after_kill(env, session, iteration: int, settings: DrillSettings, probes, ask, say, fault,
                fault_height: int) -> dict:
    """Alert, operator work, and the first valid signature after the kill, as the drill.json fields."""
    alert = probes.miss(fault_height, settings.alert_timeout_s)
    alert_ms = env.clock.now_ms() if alert is not None else None
    log_observation(session, iteration, "alert_missed_height", alert)
    if alert is not None:
        say(f"ALERT: {TARGET_VALIDATOR[:8]} missed height {alert.height}. Start the runbook.")
    else:
        say("ALERT: no missed height seen yet (alert not observed). Start the runbook.")
    ask("Press Enter when you start working: ")
    operator_start_ms = env.clock.now_ms()
    session.events.append("operator_start", t_ms=operator_start_ms, iteration=iteration, operator=settings.operator)
    commands = _operator_commands(env, session, iteration, ask, say)
    signed_after_ms = max(fault.ended_ms, alert.header_time_ms + 1) if alert is not None else fault.ended_ms
    signature = probes.signature(signed_after_ms, fault_height, settings.signature_timeout_s)
    log_observation(session, iteration, "first_signature", signature)
    signature_ms = signature.signers.get(TARGET_VALIDATOR) if signature is not None else None
    return {
        "operator": settings.operator,
        "iteration": iteration,
        "outcome": "recovered" if signature is not None else "no_signature",
        "recovery_definition": "t_first_signature_ms minus t_fault_ms (the instant the kill command was issued); "
                               "the signature must be cast after the kill command ended and after the alert height",
        "t_fault_ms": fault.started_ms,
        "t_fault_ended_ms": fault.ended_ms,
        "alert_observed": alert is not None,
        "alert_height": alert.height if alert is not None else None,
        "t_alert_ms": alert_ms,
        "t_operator_start_ms": operator_start_ms,
        "commands": commands,
        "first_signature_height": signature.height if signature is not None else None,
        "t_first_signature_ms": signature_ms,
        "alert_s": seconds_between(fault.started_ms, alert_ms),
        "reaction_s": seconds_between(alert_ms, operator_start_ms),
        "recovery_s": seconds_between(fault.started_ms, signature_ms),
    }
