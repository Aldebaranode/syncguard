"""Command line: thin handlers, exit codes, and the dry run."""

import io
import json

from fakes import (
    FIXTURES,
    TARGET_ADDRESS,
    FakeClock,
    FakeCommands,
    make_session,
    metadata_http,
    metadata_script,
    paths_under,
    ready_env,
    write_configs,
    write_key,
)
from harness.__main__ import main, parse_iterations
from harness.dryrun import build_fixture_session
from harness.environment import build_environment
from harness.scenarios.common import PreconditionError


def test_parse_iterations():
    """Comma lists become sets; empty text is an empty set."""
    assert parse_iterations("1, 5,10") == {1, 5, 10}
    assert parse_iterations("") == set()


def test_dry_run_writes_the_reports(tmp_path, capsys):
    """The dry run builds SFIX from fixtures and writes tables, figures, and values.json."""
    assert main(["dry-run", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "results" / "tabel-4-3-failover.md").exists()
    assert (tmp_path / "results" / "gambar-failover-SFIX-iterasi-1.png").exists()
    assert (tmp_path / "results" / "values.json").exists()
    output = capsys.readouterr().out
    assert "tabel-4-3-failover.md" in output and "values.json" in output


def test_start_prints_the_new_session_id(tmp_path, capsys):
    """start creates S4 through the domain function."""
    paths = paths_under(tmp_path)
    write_configs(paths, "cli-test-secret")
    clock = FakeClock(1791169200000)
    env = build_environment(metadata_http(), FakeCommands(metadata_script(paths.syncguard_dir, paths.localnet_dir), clock),
                            clock, paths, 25200)
    assert main(["start", "--purpose", "uji"], env_factory=lambda: env) == 0
    assert capsys.readouterr().out.strip() == "S4"


def test_unknown_session_exits_1(tmp_path, capsys):
    """A run needs a started session."""
    env = ready_env(tmp_path)
    assert main(["run", "security", "--session", "S9"], env_factory=lambda: env) == 1
    assert "python -m harness start" in capsys.readouterr().err


def test_refused_run_exits_2_and_lists_failures(tmp_path, capsys):
    """Precondition failures are printed one per line."""
    env = ready_env(tmp_path)
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    make_session(env.paths.data_dir, env.clock)
    assert main(["run", "failover", "--session", "S4"], env_factory=lambda: env) == 2
    err = capsys.readouterr().err
    assert "refused: failover" in err
    assert "backup key file holds the validator key" in err


def test_drill_needs_an_interactive_terminal(tmp_path, capsys, monkeypatch):
    """A stdin that is not a terminal makes the drill refuse before any check."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    env = ready_env(tmp_path, ps_output="")
    make_session(env.paths.data_dir, env.clock)
    assert main(["run", "drill", "--session", "S4", "--operator", "Heed"], env_factory=lambda: env) == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert not any(call[:2] == ["docker", "kill"] for call in env.commands.calls)


def capture_calls(monkeypatch, name: str, result) -> list:
    """Replace a domain function of harness.__main__ with a recorder that returns result."""
    calls = []

    def recorder(*args, **kwargs):
        """Record the call."""
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(f"harness.__main__.{name}", recorder)
    return calls


def test_failover_handler_delegates_with_probes_and_monitor(tmp_path, monkeypatch, capsys):
    """run failover passes the count, the snapshot iterations, live probes, and the dual-signer monitor to one domain call."""
    env = ready_env(tmp_path)
    make_session(env.paths.data_dir, env.clock)
    calls = capture_calls(monkeypatch, "run_failover_series", ["complete"])
    argv = ["run", "failover", "--session", "S4", "--iterations", "3", "--snapshot-iterations", "1,3"]
    assert main(argv, env_factory=lambda: env) == 0
    (environment, session, count, _settings, snapshots, probes, monitor), _ = calls[0]
    assert (environment, session.id, count, snapshots) == (env, "S4", 3, {1, 3})
    assert type(probes).__name__ == "LiveProbes" and type(monitor).__name__ == "DualSignerMonitor"
    assert json.loads(capsys.readouterr().out) == ["complete"]


def test_drill_handler_passes_the_monitor_input_and_print(tmp_path, monkeypatch):
    """The drill gets a dual-signer monitor, input() and print() for its prompts, and the operator's name."""
    monkeypatch.setattr("sys.stdin", type("Tty", (io.StringIO,), {"isatty": lambda self: True})(""))
    env = ready_env(tmp_path)
    make_session(env.paths.data_dir, env.clock)
    calls = capture_calls(monkeypatch, "run_drill", {"outcome": "recovered"})
    assert main(["run", "drill", "--session", "S4", "--operator", "Heed"], env_factory=lambda: env) == 0
    (_env, session, settings, probes, monitor, ask, say), _ = calls[0]
    assert (session.id, settings.operator, ask, say) == ("S4", "Heed", input, print)
    assert type(probes).__name__ == "LiveProbes" and type(monitor).__name__ == "DualSignerMonitor"


def test_overhead_soak_and_hang_handlers_translate_their_options(tmp_path, monkeypatch):
    """--minutes becomes seconds, --hours and --kill-offset-hours reach SoakSettings, and a hang takes the next iteration."""
    env = ready_env(tmp_path)
    make_session(env.paths.data_dir, env.clock)
    overhead = capture_calls(monkeypatch, "run_overhead", {})
    soak = capture_calls(monkeypatch, "run_soak", {})
    hang = capture_calls(monkeypatch, "run_hang", {})
    assert main(["run", "overhead", "--session", "S4", "--minutes", "2"], env_factory=lambda: env) == 0
    assert main(["run", "soak", "--session", "S4", "--hours", "3", "--kill-offset-hours", "1"], env_factory=lambda: env) == 0
    assert main(["run", "hang", "--session", "S4"], env_factory=lambda: env) == 0
    assert overhead[0][0][2].duration_s == 120.0
    assert (soak[0][0][2].hours, soak[0][0][2].kill_offset_hours) == (3.0, 1.0)
    assert hang[0][0][2] == 1


def test_a_domain_value_error_exits_1(tmp_path, monkeypatch, capsys):
    """A ValueError from the domain layer is a bad input: exit 1 with its message."""
    env = ready_env(tmp_path)
    make_session(env.paths.data_dir, env.clock)

    def refuse(*args, **kwargs):
        """Raise as an unsafe config would."""
        raise ValueError("unsafe config")

    monkeypatch.setattr("harness.__main__.run_lock_negative", refuse)
    assert main(["run", "lock", "--session", "S4"], env_factory=lambda: env) == 1
    assert "error: unsafe config" in capsys.readouterr().err


def test_a_precondition_error_from_a_domain_call_exits_2(tmp_path, monkeypatch, capsys):
    """Failures raised inside a runner are listed on stderr one per line."""
    env = ready_env(tmp_path)
    make_session(env.paths.data_dir, env.clock)

    def refuse(*args, **kwargs):
        """Raise as a runner does."""
        raise PreconditionError("lock_negative", ["first problem", "second problem"])

    monkeypatch.setattr("harness.__main__.run_lock_negative", refuse)
    assert main(["run", "lock", "--session", "S4"], env_factory=lambda: env) == 2
    assert "  - second problem" in capsys.readouterr().err


def test_analyze_and_report_commands(tmp_path, capsys):
    """analyze then report on a data directory."""
    build_fixture_session(FIXTURES, tmp_path / "data")
    assert main(["analyze", "--session", "SFIX", "--data-dir", str(tmp_path / "data")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"session": "SFIX", "failover": 1, "failback": 1, "hang": 0}
    assert main(["report", "SFIX", "--data-dir", str(tmp_path / "data"), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "index.md").exists()
    assert (tmp_path / "out" / "values.json").exists()


def test_report_without_analysis_exits_1_with_the_next_step(tmp_path, capsys):
    """A session that was never analyzed names the analyze command."""
    build_fixture_session(FIXTURES, tmp_path / "data")
    assert main(["report", "SFIX", "--data-dir", str(tmp_path / "data"), "--out", str(tmp_path / "out")]) == 1
    assert "python -m harness analyze --session SFIX" in capsys.readouterr().err
