"""failover_cycle (R9): docker kill the primary, follow takeover and failback, then collect R3-R8 data."""

from dataclasses import dataclass, replace

from harness.constants import BACKUP_NODE, PRIMARY_CONTAINER, TIP_MARGIN
from harness.preconditions import failover_preconditions
from harness.scenarios.common import (
    abort_recorded,
    collect_heights_in_chunks,
    container_status,
    cycle_outcome,
    docker_action,
    follow_failback,
    log_observation,
    record_window,
    require,
    run_series,
)
from harness.snapshots import take_snapshot

SCENARIO = "failover"


@dataclass(frozen=True)
class FailoverSettings:
    """Waits and collection settings of one failover iteration."""

    takeover_timeout_s: float = 180.0
    signature_timeout_s: float = 180.0
    failback_timeout_s: float = 300.0
    stabilization_s: float = 100.0
    heights_before_fault: int = 30
    snapshots: bool = True


def run_failover_iteration(env, session, iteration: int, settings: FailoverSettings, probes) -> str:
    """One failover-and-failback cycle: preconditions, docker kill, waits, snapshots, then collection."""
    started_ms = env.clock.now_ms()
    require(session, SCENARIO, failover_preconditions(env), iteration)
    session.events.append("iteration_start", scenario=SCENARIO, iteration=iteration)
    first_height = max(1, env.rpc.tip_height() - settings.heights_before_fault)
    if settings.snapshots:
        take_snapshot(env, session, iteration, "before_fault")
    fault_height = env.rpc.tip_height()
    fault = docker_action(env, session, "kill", PRIMARY_CONTAINER, SCENARIO, iteration, "fault_injected",
                          height=fault_height)

    def record_log_window() -> None:
        """Record the syncguard log window up to now for an aborted iteration."""
        record_window(env, session, iteration, started_ms, env.clock.now_ms())

    with abort_recorded(session, SCENARIO, iteration, record_log_window):
        outcome = _follow_fault(env, session, iteration, settings, probes, fault, fault_height)
        env.clock.sleep(settings.stabilization_s)
        session.events.append("stabilization", iteration=iteration, seconds=settings.stabilization_s)
    record_window(env, session, iteration, started_ms, env.clock.now_ms())
    session.events.append("iteration_result", scenario=SCENARIO, iteration=iteration, outcome=outcome)
    with abort_recorded(session, SCENARIO, iteration):
        collect_heights_in_chunks(env, session, first_height, env.rpc.tip_height() - TIP_MARGIN)
    session.events.append("iteration_end", scenario=SCENARIO, iteration=iteration, outcome=outcome)
    return outcome


def _follow_fault(env, session, iteration: int, settings: FailoverSettings, probes, fault, fault_height: int) -> str:
    """Follow the cycle when the kill took effect; a failed kill command is trusted only if the container is stopped."""
    if fault.exit_code != 0:
        state = container_status(env, PRIMARY_CONTAINER)
        session.events.append("fault_command_failed", iteration=iteration, exit_code=fault.exit_code,
                              container_state=state)
        if state != "exited":
            return "fault_command_failed"
    return _follow_cycle(env, session, iteration, settings, probes, fault, fault_height)


def _follow_cycle(env, session, iteration: int, settings: FailoverSettings, probes, fault, fault_height: int) -> str:
    """Wait for takeover, the backup's first signature, failback, the release, and the primary's first signature.

    Log markers are searched from the instant the kill was issued; signatures only from the instant the
    kill command ended, so a precommit cast before the kill landed cannot count as post-fault.
    """
    fault_ms, signed_after_ms = fault.started_ms, fault.ended_ms
    takeover = probes.marker(BACKUP_NODE, "takeover_complete", fault_ms, settings.takeover_timeout_s)
    log_observation(session, iteration, "takeover_complete", takeover)
    if takeover is None:
        return "no_takeover"
    backup_signature = probes.signature(signed_after_ms, fault_height, settings.signature_timeout_s)
    log_observation(session, iteration, "backup_first_signature", backup_signature)
    if settings.snapshots:
        take_snapshot(env, session, iteration, "after_takeover")
    failback, released, primary_signature = follow_failback(session, iteration, probes, takeover, fault_height,
                                                            settings.failback_timeout_s, settings.signature_timeout_s)
    if settings.snapshots and released is not None:
        take_snapshot(env, session, iteration, "after_failback")
    return cycle_outcome(takeover, backup_signature, failback, released, primary_signature)


def run_failover_series(env, session, count: int, settings: FailoverSettings, snapshot_iterations: set,
                        probes, monitor) -> list:
    """Run count iterations with the dual-signer monitor running around all of them (common.run_series).

    A refusal ends the series by raising; any outcome other than complete stops it too, logged as
    series_aborted. Iterations that completed before it stay valid.
    """

    def run_one(iteration: int) -> str:
        """One iteration, taking snapshots only when the iteration was chosen for them."""
        per_iteration = replace(settings, snapshots=iteration in snapshot_iterations)
        return run_failover_iteration(env, session, iteration, per_iteration, probes)

    return run_series(session, SCENARIO, count, monitor, run_one, lambda outcome: outcome)
