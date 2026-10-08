"""Building blocks shared by the scenario runners."""

import json
from contextlib import contextmanager

from harness.adapters import Unreachable
from harness.constants import BACKUP_NODE, PRIMARY_NODE
from harness.logparse import LogEvent, parse_log_file
from harness.monitor import UNREACHABLE
from harness.session import read_events
from harness.signing import collect_heights, load_heights, store_heights


KILLED_STATES = ("exited", "dead")  # docker states in which the primary holds no key


class PreconditionError(Exception):
    """A runner refused to start; failures lists every unmet precondition."""

    def __init__(self, scenario: str, failures: list):
        """Keep the scenario name and the failure messages."""
        super().__init__(f"{scenario} refused: " + "; ".join(failures))
        self.scenario = scenario
        self.failures = failures


def require(session, scenario: str, failures: list, iteration: int | None = None) -> None:
    """Log the precondition outcome; raise PreconditionError when any precondition failed."""
    if failures:
        session.events.append("precondition_failed", scenario=scenario, iteration=iteration, failures=failures)
        raise PreconditionError(scenario, failures)
    session.events.append("preconditions_passed", scenario=scenario, iteration=iteration)


def next_iteration(session) -> int:
    """One more than the highest iteration started in this session."""
    rows = read_events(session.directory / "events.jsonl")
    numbers = [row["iteration"] for row in rows if row["kind"] == "iteration_start"]
    return max(numbers, default=0) + 1


def docker_command(env, session, argv: list, action: str, container: str, scenario: str, iteration: int, kind: str,
                   **fields):
    """Run a docker command and log it, stamped with the instant the command was issued.

    action names what the command does (pause, network disconnect); argv is the exact command.
    """
    result = env.commands.run(argv, timeout=120)
    session.events.append(
        kind,
        t_ms=result.started_ms,
        scenario=scenario,
        iteration=iteration,
        action=action,
        container=container,
        command=" ".join(result.argv),
        exit_code=result.exit_code,
        ended_ms=result.ended_ms,
        stderr=result.stderr.strip(),
        **fields,
    )
    return result


def docker_action(env, session, action: str, container: str, scenario: str, iteration: int, kind: str, **fields):
    """Run `docker <action> <container>` and log it, stamped with the instant the command was issued."""
    return docker_command(env, session, ["docker", action, container], action, container, scenario, iteration, kind,
                          **fields)


def container_endpoints(env, container: str) -> dict | None:
    """The docker networks a container is attached to, or None when docker cannot say.

    Maps each network name to its endpoint: ip (the static IPAMConfig address, or None), address (the
    address in use) and aliases. The names come from `docker inspect`, never from a constant.
    """
    result = env.commands.run(["docker", "inspect", container, "--format", "{{json .NetworkSettings.Networks}}"])
    if result.exit_code != 0:
        return None
    try:
        networks = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(networks, dict):
        return None
    endpoints = {}
    for name, details in networks.items():
        if not isinstance(details, dict):
            return None
        config = details.get("IPAMConfig") or {}
        endpoints[name] = {"ip": config.get("IPv4Address"), "address": details.get("IPAddress") or None,
                           "aliases": list(details.get("Aliases") or [])}
    return endpoints


def container_networks(env, container: str) -> list | None:
    """The sorted names of the docker networks a container is attached to, or None when docker cannot say."""
    endpoints = container_endpoints(env, container)
    return None if endpoints is None else sorted(endpoints)


def best_effort(session, scenario: str, iteration: int, step: str, action):
    """Run action(); a failure, even a second Ctrl-C, is logged as cleanup_failed with the step and returns (False, None)."""
    try:
        return True, action()
    except BaseException as error:
        session.events.append("cleanup_failed", scenario=scenario, iteration=iteration, step=step, error=repr(error))
        return False, None


def two_attempts(session, scenario: str, iteration: int, step: str, attempt_action):
    """The cleanup retry shared by the runners: attempt_action(attempt) for attempts 1 and 2 until one exits 0.

    Each attempt is best effort, so a second Ctrl-C is logged as cleanup_failed with '<step> attempt <n>'.
    Returns the successful command result, or None after logging cleanup_failed with the step when
    neither attempt completed.
    """
    for attempt in (1, 2):
        done, result = best_effort(session, scenario, iteration, f"{step} attempt {attempt}",
                                   lambda: attempt_action(attempt))
        if done and result.exit_code == 0:
            return result
    session.events.append("cleanup_failed", scenario=scenario, iteration=iteration, step=step,
                          error=f"the {step} did not complete after two attempts")
    return None


def log_observation(session, iteration: int, waiting_for: str, found) -> None:
    """Log what a wait found (a log event or a signed height), or that it timed out."""
    if found is None:
        session.events.append("wait_timeout", iteration=iteration, waiting_for=waiting_for)
    elif isinstance(found, LogEvent):
        session.events.append("observed", iteration=iteration, waiting_for=waiting_for,
                              observed_ms=found.t_ms, source=found.source)
    else:
        session.events.append("observed", iteration=iteration, waiting_for=waiting_for, height=found.height)


def append_log_events(event_log, log_events: list, iteration: int) -> None:
    """Append LogEvents to an event log as syncguard rows tagged with the iteration, in time order."""
    for event in sorted(log_events, key=lambda item: item.t_ms):
        fields = event.to_dict()
        t_ms = fields.pop("t_ms")
        event_log.append("syncguard", t_ms=t_ms, iteration=iteration, **fields)


def record_window(env, session, iteration: int, since_ms: int, until_ms: int) -> list:
    """Append every syncguard event of both logs inside [since_ms, until_ms] to events.jsonl with the iteration."""
    events = []
    for path, node in ((env.paths.active_log, PRIMARY_NODE), (env.paths.passive_log, BACKUP_NODE)):
        if path.exists():
            events += parse_log_file(path, node, env.utc_offset_seconds, since_ms, until_ms)
    append_log_events(session.events, events, iteration)
    return events


def record_heights(env, session, first: int, last: int) -> list:
    """Collect [first, last] from rpc1, keep the new heights in heights.jsonl, and log the range."""
    records = collect_heights(env.rpc, first, last)
    store_heights(session.directory / "heights.jsonl", session.id, records)
    session.events.append("heights_collected", first=first, last=last)
    return records


def container_status(env, container: str) -> str:
    """docker inspect State.Status (running, paused, exited, ...), or 'unknown'."""
    result = env.commands.run(["docker", "inspect", "-f", "{{.State.Status}}", container])
    return result.stdout.strip() if result.exit_code == 0 else "unknown"


def loaded_address_or_unreachable(rpc) -> str:
    """The address a node loaded, or UNREACHABLE."""
    try:
        return rpc.loaded_address()
    except Unreachable:
        return UNREACHABLE


@contextmanager
def abort_recorded(session, scenario: str, iteration: int | None, cleanup=None, kind: str = "iteration"):
    """Log `<kind>_aborted` (any exception) or `<kind>_interrupted` (Ctrl-C) with the error text, run cleanup, re-raise.

    cleanup is best effort (for example recording the syncguard log window); a failure
    there is logged and never hides the original error.
    """
    try:
        yield
    except KeyboardInterrupt:
        _log_abort(session, f"{kind}_interrupted", scenario, iteration, "KeyboardInterrupt", cleanup)
        raise
    except Exception as error:
        _log_abort(session, f"{kind}_aborted", scenario, iteration, repr(error), cleanup)
        raise


def _log_abort(session, event_kind: str, scenario: str, iteration: int | None, error: str, cleanup) -> None:
    """Append the abort event, then run the cleanup without letting it raise."""
    session.events.append(event_kind, scenario=scenario, iteration=iteration, error=error)
    if cleanup is None:
        return
    try:
        cleanup()
    except BaseException as cleanup_error:  # a second Ctrl-C must not hide the original error
        session.events.append("cleanup_failed", scenario=scenario, iteration=iteration, error=repr(cleanup_error))


def collect_heights_in_chunks(env, session, first: int, last: int, chunk_size: int = 500, attempts: int = 3,
                              retry_wait_s: float = 5.0) -> list:
    """Collect [first, last] chunk by chunk into heights.jsonl; stored chunks are skipped, so a rerun resumes.

    A chunk that fails with Unreachable is retried; after the last attempt the error is raised
    and every earlier chunk stays stored.
    """
    path = session.directory / "heights.jsonl"
    for chunk_first in range(first, last + 1, chunk_size):
        chunk_last = min(chunk_first + chunk_size - 1, last)
        stored = {record.height for record in load_heights(path)}
        missing = [height for height in range(chunk_first, chunk_last + 1) if height not in stored]
        if not missing:
            continue
        for attempt in range(1, attempts + 1):
            try:
                store_heights(path, session.id, collect_heights(env.rpc, missing[0], missing[-1]))
                break
            except Unreachable as error:
                session.events.append("heights_retry", first=missing[0], last=missing[-1], attempt=attempt,
                                      error=str(error))
                if attempt == attempts:
                    raise
                env.clock.sleep(retry_wait_s)
    session.events.append("heights_collected", first=first, last=last)
    return [record for record in load_heights(path) if first <= record.height <= last]


def follow_failback(session, iteration: int, probes, takeover, fault_height: int, failback_timeout_s: float,
                    signature_timeout_s: float) -> tuple:
    """(failback, release, primary signature) after a takeover; each is None when it did not happen in time.

    The failback and the release are searched from the takeover, the primary's first signature from the
    release; each wait runs only when the one before it succeeded, and every wait is logged.
    """
    if takeover is None:
        return None, None, None
    failback = probes.marker(PRIMARY_NODE, "failback_complete", takeover.t_ms, failback_timeout_s)
    log_observation(session, iteration, "failback_complete", failback)
    if failback is None:
        return None, None, None
    released = probes.marker(BACKUP_NODE, "release_complete", takeover.t_ms, failback_timeout_s)
    log_observation(session, iteration, "release_complete", released)
    if released is None:
        return failback, None, None
    primary_signature = probes.signature(released.t_ms, fault_height, signature_timeout_s)
    log_observation(session, iteration, "primary_first_signature", primary_signature)
    return failback, released, primary_signature


def cycle_outcome(takeover, backup_signature, failback, released, primary_signature) -> str:
    """The first step of a takeover-and-failback cycle that did not happen, or complete."""
    if takeover is None:
        return "no_takeover"
    if failback is None:
        return "no_failback"
    if released is None:
        return "no_release"
    if backup_signature is None or primary_signature is None:
        return "missing_signature"
    return "complete"


def run_series(session, scenario: str, count: int, monitor, run_one, outcome_of) -> list:
    """Run count iterations with one dual-signer monitor around all of them; the one series rule of the runners.

    run_one(iteration) runs one iteration and returns its result; outcome_of(result) names its outcome.
    Any outcome other than complete stops the series, logged as series_aborted; a refusal or an abort
    ends it by raising. Iterations that completed before it stay valid, and the monitor always stops.
    """
    first = next_iteration(session)
    results = []
    monitor.start()
    try:
        for iteration in range(first, first + count):
            result = run_one(iteration)
            results.append(result)
            outcome = outcome_of(result)
            if outcome != "complete":
                session.events.append("series_aborted", scenario=scenario, iteration=iteration, outcome=outcome)
                break
    finally:
        monitor.stop()
    return results
