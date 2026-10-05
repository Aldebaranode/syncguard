"""Command line: python -m harness <command>, run from experiments/.

Each handler parses its arguments, wires the live adapters, calls one domain
function, and prints the result. Refusals exit with 2, missing inputs with 1.
"""

import argparse
import json
import sys
from pathlib import Path

from harness.analysis import analyze_session
from harness.constants import ARCHIVED_SESSIONS_CSV, DATA_DIR, RESULTS_DIR
from harness.dryrun import run_dry
from harness.environment import live_environment
from harness.finish import collect_safety
from harness.monitor import DualSignerMonitor
from harness.outputs import write_outputs
from harness.scenarios.common import PreconditionError, next_iteration
from harness.scenarios.drill import DrillSettings, run_drill
from harness.scenarios.failover import FailoverSettings, run_failover_series
from harness.scenarios.hang import HangSettings, run_hang
from harness.scenarios.negatives import run_lock_negative, run_security_negatives
from harness.scenarios.overhead import OverheadSettings, run_overhead
from harness.scenarios.soak import SoakSettings, run_soak
from harness.session import open_session, start_session
from harness.waits import LiveProbes


def run_failover_command(args, env, session):
    """failover_cycle series with the live probes and the dual-signer monitor."""
    return run_failover_series(env, session, args.iterations, FailoverSettings(), parse_iterations(args.snapshot_iterations),
                               LiveProbes(env, session), DualSignerMonitor.for_session(env, session))


def run_hang_command(args, env, session):
    """One hang iteration."""
    return run_hang(env, session, next_iteration(session), HangSettings(), LiveProbes(env, session),
                    DualSignerMonitor.for_session(env, session))


def run_security_command(args, env, session):
    """The seven negative requests and the correct-HMAC control."""
    return run_security_negatives(env, session)


def run_lock_command(args, env, session):
    """The lock negative test."""
    return run_lock_negative(env, session)


def run_overhead_command(args, env, session):
    """Overhead sampling for --minutes."""
    return run_overhead(env, session, OverheadSettings(duration_s=args.minutes * 60))


def run_drill_command(args, env, session):
    """The baseline drill; it needs a human at an interactive terminal and a monitor that runs until the restore."""
    if not sys.stdin.isatty():
        raise PreconditionError("baseline_drill", ["no interactive terminal: a human operator must run the drill"])
    return run_drill(env, session, DrillSettings(operator=args.operator), LiveProbes(env, session),
                     DualSignerMonitor.for_session(env, session), input, print)


def run_soak_command(args, env, session):
    """The soak with one failover."""
    return run_soak(env, session, SoakSettings(hours=args.hours, kill_offset_hours=args.kill_offset_hours),
                    FailoverSettings(), LiveProbes(env, session), DualSignerMonitor.for_session(env, session))


SCENARIO_HANDLERS = {
    "failover": run_failover_command,
    "hang": run_hang_command,
    "security": run_security_command,
    "lock": run_lock_command,
    "overhead": run_overhead_command,
    "drill": run_drill_command,
    "soak": run_soak_command,
}


def build_parser() -> argparse.ArgumentParser:
    """Arguments of every command."""
    parser = argparse.ArgumentParser(prog="python -m harness", description="Telaah 2 measurement harness for syncguard.")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="start a new session and write metadata.json")
    start.add_argument("--purpose", required=True)

    run = commands.add_parser("run", help="run one scenario inside a started session")
    run.add_argument("scenario", choices=tuple(SCENARIO_HANDLERS))
    run.add_argument("--session", required=True)
    run.add_argument("--iterations", type=int, default=1)
    run.add_argument("--snapshot-iterations", default="", help="comma-separated iterations that take snapshots")
    run.add_argument("--minutes", type=float, default=10.0, help="overhead sampling duration")
    run.add_argument("--operator", default="", help="name of the human operator (drill)")
    run.add_argument("--hours", type=float, default=24.0, help="soak duration")
    run.add_argument("--kill-offset-hours", type=float, default=6.0, help="soak fault offset")

    finish = commands.add_parser("finish", help="evidence scan, signing info, Prometheus, monitor summary, log copies")
    finish.add_argument("--session", required=True)

    analyze = commands.add_parser("analyze", help="compute analysis.json for one session")
    analyze.add_argument("--session", required=True)
    analyze.add_argument("--data-dir", default=str(DATA_DIR))

    report = commands.add_parser("report", help="write tables, figures, and values.json for analyzed sessions")
    report.add_argument("sessions", nargs="+")
    report.add_argument("--data-dir", default=str(DATA_DIR))
    report.add_argument("--out", default=str(RESULTS_DIR))

    dry = commands.add_parser("dry-run", help="analyze and report the recorded fixtures without the localnet")
    dry.add_argument("--out", required=True)
    return parser


def parse_iterations(text: str) -> set:
    """'1,5,10' to {1, 5, 10}; empty text to an empty set."""
    return {int(part) for part in text.split(",") if part.strip()}


def show(result) -> None:
    """Print a result as JSON."""
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


def handle_start(args, env_factory) -> int:
    """Start a session and print its id."""
    print(start_session(env_factory(), args.purpose))
    return 0


def handle_run(args, env_factory) -> int:
    """Open the session and run the chosen scenario."""
    env = env_factory()
    session = open_session(env.paths.data_dir, args.session, env.clock)
    show(SCENARIO_HANDLERS[args.scenario](args, env, session))
    return 0


def handle_finish(args, env_factory) -> int:
    """Collect the session-end safety evidence."""
    env = env_factory()
    session = open_session(env.paths.data_dir, args.session, env.clock)
    show(collect_safety(env, session))
    return 0


def handle_analyze(args, env_factory) -> int:
    """Analyze one session and print how many iterations it holds."""
    directory = Path(args.data_dir) / args.session
    if not (directory / "metadata.json").exists():
        raise FileNotFoundError(f"{directory} has no metadata.json")
    result = analyze_session(directory)
    show({"session": result["session"], "failover": len(result["failover"]), "failback": len(result["failback"]),
          "hang": len(result["hang"])})
    return 0


def handle_report(args, env_factory) -> int:
    """Write the tables, figures, and values.json and print their paths."""
    directories = [Path(args.data_dir) / session for session in args.sessions]
    for path in write_outputs(directories, Path(args.out), ARCHIVED_SESSIONS_CSV):
        print(path)
    return 0


def handle_dry_run(args, env_factory) -> int:
    """Run the fixture dry run and print the written paths."""
    for path in run_dry(Path(args.out)):
        print(path)
    return 0


HANDLERS = {
    "start": handle_start,
    "run": handle_run,
    "finish": handle_finish,
    "analyze": handle_analyze,
    "report": handle_report,
    "dry-run": handle_dry_run,
}


def main(argv=None, env_factory=live_environment) -> int:
    """Parse argv and dispatch; refusals exit 2 with every failure listed, missing inputs exit 1."""
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args, env_factory)
    except PreconditionError as error:
        print(f"refused: {error.scenario}", file=sys.stderr)
        for failure in error.failures:
            print(f"  - {failure}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
