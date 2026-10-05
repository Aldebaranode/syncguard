"""Preconditions: every runner refuses a wrong environment instead of repairing it."""

from pathlib import Path

from fakes import (
    MOCK_ADDRESS,
    PS_OUTPUT,
    TARGET_ADDRESS,
    FakeClock,
    load_fixture,
    ready_env,
    signed_commit_answer,
    write_key,
)
from harness.adapters import HttpResponse, Unreachable
from harness.logparse import parse_log_file
from harness.preconditions import (
    drill_preconditions,
    failover_preconditions,
    security_preconditions,
    startup_state,
    syncguard_pids,
)

STOP_LINE = 'time="2026-10-05 10:00:00.000" level=info msg="Received signal terminated. Shutting down..."\n'


def test_ready_environment_passes(tmp_path):
    """All eight failover checks pass on the ready fake environment."""
    assert failover_preconditions(ready_env(tmp_path)) == []


def test_backup_key_holding_the_validator_key_is_refused(tmp_path):
    """Two key files with the validator key means two possible signers."""
    env = ready_env(tmp_path)
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    assert any("backup key file" in failure for failure in failover_preconditions(env))


def test_backup_node_running_the_validator_key_is_refused(tmp_path):
    """The loaded key matters, not only the file."""
    env = ready_env(tmp_path, extra_http={"http://localhost:22657/status": load_fixture("rpc/status_validator1.json")})
    assert "validator2-node loaded the validator key" in failover_preconditions(env)


def test_stopped_syncguard_is_refused(tmp_path):
    """A shutdown line after the latest start means the process is gone."""
    env = ready_env(tmp_path)
    with env.paths.active_log.open("a") as handle:
        handle.write(STOP_LINE)
    assert "syncguard.log: latest syncguard process is stopped" in failover_preconditions(env)


def test_missing_process_and_foreign_lock_owner_are_refused(tmp_path):
    """Only the active syncguard runs, and the lock names another PID."""
    env = ready_env(tmp_path, ps_output="  4242 ./bin/syncguard --config config.yaml --role active\n")
    Path(f"{env.paths.primary_state}.lock").write_text("999\n")
    failures = failover_preconditions(env)
    assert "syncguard not running for validator-2" in failures
    assert "primary state lock names PID 999, active syncguard PID is 4242" in failures


def test_validator_not_signing_is_refused(tmp_path):
    """The newest readable commit lacks the validator."""
    env = ready_env(tmp_path, extra_http={"http://localhost:26657/commit?height=*": signed_commit_answer([MOCK_ADDRESS])})
    assert any("did not sign height" in failure for failure in failover_preconditions(env))


def test_halted_chain_is_refused(tmp_path):
    """The tip must rise within 6 s."""
    env = ready_env(tmp_path, extra_http={"http://localhost:26657/status": load_fixture("rpc/status_rpc1.json")})
    assert "chain not advancing: tip stayed at 1188 for 6 s" in failover_preconditions(env)


def test_recent_role_change_is_refused(tmp_path):
    """30 s after the fixture's failback, plus the 6 s chain-progress wait, is still inside the 90 s warm-up."""
    env = ready_env(tmp_path, clock=FakeClock(1791168961698 + 30000))
    assert any("last takeover or failback was 36 s ago" in failure for failure in failover_preconditions(env))


def test_unreachable_rpc_is_a_failure_not_an_exception(tmp_path):
    """Unreachable endpoints never count as a pass."""
    env = ready_env(tmp_path, extra_http={"http://localhost:26657/status": Unreachable("connection refused")})
    failures = failover_preconditions(env)
    assert any(failure.startswith("validator signing:") for failure in failures)
    assert any(failure.startswith("chain progress:") for failure in failures)


def test_drill_needs_syncguard_stopped(tmp_path):
    """The baseline drill measures recovery without syncguard."""
    running = ready_env(tmp_path / "a")
    stopped = ready_env(tmp_path / "b", ps_output="")
    assert any("syncguard is running" in failure for failure in drill_preconditions(running))
    assert drill_preconditions(stopped) == []


def test_security_needs_a_healthy_passive_peer(tmp_path):
    """The passive peer server must answer /health with 200."""
    healthy = ready_env(tmp_path / "a")
    broken = ready_env(tmp_path / "b", extra_http={"http://localhost:8081/health": HttpResponse(500, "")})
    assert security_preconditions(healthy) == []
    assert "passive syncguard /health answered 500" in security_preconditions(broken)


def test_syncguard_pids_ignore_other_processes():
    """Only executables named syncguard with --config count."""
    assert syncguard_pids(PS_OUTPUT) == {"validator-1": 4242, "validator-2": 4243}
    assert syncguard_pids("  12 /usr/bin/grep syncguard\n") == {}


def test_startup_states(tmp_path):
    """absent, starting, started, stopped."""
    lines = (Path(__file__).parent / "fixtures" / "logs" / "smoke-passive.txt").read_text().splitlines(keepends=True)
    log = tmp_path / "x.log"
    log.write_text("")
    assert startup_state(parse_log_file(log, "validator-2", 25200)) == "absent"
    log.write_text(lines[0])
    assert startup_state(parse_log_file(log, "validator-2", 25200)) == "starting"
    log.write_text("".join(lines))
    assert startup_state(parse_log_file(log, "validator-2", 25200)) == "started"
    log.write_text("".join(lines) + STOP_LINE)
    assert startup_state(parse_log_file(log, "validator-2", 25200)) == "stopped"
