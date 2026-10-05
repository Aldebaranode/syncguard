"""baseline_drill (R9, D3): operator-in-the-loop manual recovery without syncguard.

The alert instant is when the harness sees the first committed height without
a 48DC2183 signature after the kill, the trigger a missed-block alert uses.
The recovery ends at the first 48DC2183 signature after the kill.
"""

from dataclasses import dataclass

from harness.constants import PRIMARY_CONTAINER, TARGET_VALIDATOR, TIP_MARGIN
from harness.jsonio import write_json
from harness.metrics import seconds_between
from harness.preconditions import drill_preconditions
from harness.scenarios.common import docker_action, log_observation, next_iteration, record_heights, require

SCENARIO = "baseline_drill"
READY_WORD = "ready"
DONE_WORD = "done"


@dataclass(frozen=True)
class DrillSettings:
    """The operator's name and the waits of the drill."""

    operator: str
    alert_timeout_s: float = 120.0
    signature_timeout_s: float = 900.0
    heights_before_fault: int = 30


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


def run_drill(env, session, settings: DrillSettings, probes, ask, say) -> dict:
    """Kill the primary, raise the alert, record the operator's commands, and wait for the first signature."""
    iteration = next_iteration(session)
    session.events.append("iteration_start", scenario=SCENARIO, iteration=iteration)
    if not settings.operator.strip():
        require(session, SCENARIO, ["a human operator is required; pass --operator with the operator's name"], iteration)
    require(session, SCENARIO, drill_preconditions(env), iteration)
    say(f"Operator {settings.operator}: open {env.paths.runbook} in a second window.")
    if ask(f"Type '{READY_WORD}' to kill {PRIMARY_CONTAINER} and start the clock: ").strip() != READY_WORD:
        require(session, SCENARIO, ["the operator did not confirm the start"], iteration)
    first_height = max(1, env.rpc.tip_height() - settings.heights_before_fault)
    fault_height = env.rpc.tip_height()
    fault = docker_action(env, session, "kill", PRIMARY_CONTAINER, SCENARIO, iteration, "fault_injected",
                          height=fault_height)
    alert = probes.miss(fault_height, settings.alert_timeout_s)
    alert_ms = env.clock.now_ms()
    log_observation(session, iteration, "alert_missed_height", alert)
    if alert is not None:
        say(f"ALERT: {TARGET_VALIDATOR[:8]} missed height {alert.height}. Start the runbook.")
    else:
        say("ALERT: no missed height seen yet. Start the runbook.")
    ask("Press Enter when you start working: ")
    operator_start_ms = env.clock.now_ms()
    session.events.append("operator_start", t_ms=operator_start_ms, iteration=iteration, operator=settings.operator)
    commands = _operator_commands(env, session, iteration, ask, say)
    signature = probes.signature(fault.started_ms, fault_height, settings.signature_timeout_s)
    log_observation(session, iteration, "first_signature", signature)
    record_heights(env, session, first_height, env.rpc.tip_height() - TIP_MARGIN)
    signature_ms = signature.signers.get(TARGET_VALIDATOR) if signature is not None else None
    result = {
        "session": session.id,
        "operator": settings.operator,
        "iteration": iteration,
        "outcome": "recovered" if signature is not None else "no_signature",
        "t_fault_ms": fault.started_ms,
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
    write_json(session.directory / "drill.json", result)
    session.events.append("iteration_end", scenario=SCENARIO, iteration=iteration, outcome=result["outcome"])
    return result
