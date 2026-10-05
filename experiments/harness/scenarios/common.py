"""Building blocks shared by the scenario runners."""

from harness.adapters import Unreachable
from harness.constants import BACKUP_NODE, PRIMARY_NODE
from harness.logparse import LogEvent, parse_log_file
from harness.monitor import UNREACHABLE
from harness.session import read_events
from harness.signing import collect_heights, store_heights


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


def docker_action(env, session, action: str, container: str, scenario: str, iteration: int, kind: str, **fields):
    """Run `docker <action> <container>` and log it, stamped with the instant the command was issued."""
    result = env.commands.run(["docker", action, container], timeout=120)
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
