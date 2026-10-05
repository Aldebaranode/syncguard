"""hang runner: docker pause, detection, and a conservative, verified end of the hang."""

import pytest

from fakes import (
    MOCK_ADDRESS,
    TARGET_ADDRESS,
    FakeMonitor,
    FakeProbes,
    load_fixture,
    make_session,
    ready_env,
    rising_status,
    signed_commit_answer,
    write_key,
)
from harness.adapters import Unreachable
from harness.jsonio import read_json
from harness.logparse import LogEvent
from harness.monitor import UNREACHABLE
from harness.scenarios.common import PreconditionError
from harness.scenarios.hang import HangSettings, clearing_action, restart_outcome, run_hang
from harness.session import read_events

PAUSE = ("docker", "pause", "validator1-node")
UNPAUSE = ("docker", "unpause", "validator1-node")
KILL = ("docker", "kill", "validator1-node")
INSPECT = ("docker", "inspect", "-f", "{{.State.Status}}", "validator1-node")
LAST_SIGNED_HEIGHT = 1190  # heights above this are read after the pause began


def validator_stops_signing(url: str):
    """Commits in which the validator signs up to LAST_SIGNED_HEIGHT and only the mock key signs after."""
    height = int(url.rsplit("=", 1)[1])
    signers = [TARGET_ADDRESS, MOCK_ADDRESS] if height <= LAST_SIGNED_HEIGHT else [MOCK_ADDRESS]
    return signed_commit_answer(signers)(url)


MOCK_ONLY_COMMITS = {"http://localhost:26657/commit?height=*": validator_stops_signing}


def status(*states):
    """A scripted docker inspect answer sequence."""
    return [(0, f"{state}\n", "") for state in states]


def log_event(marker, t_ms, source):
    """A primary-node LogEvent at t_ms."""
    return LogEvent(marker, "validator-1", t_ms, "error", "m", source)


def aborted_probes(pause_ms):
    """Detection at 33 s, a failed restart, and an aborted failover (no handover)."""
    return FakeProbes({
        ("validator-1", "max_failures"): log_event("max_failures", pause_ms + 33000, "syncguard.log:80"),
        ("validator-1", "restart_failed"): log_event("restart_failed", pause_ms + 40000, "syncguard.log:85"),
        ("validator-1", "failover_aborted"): log_event("failover_aborted", pause_ms + 60000, "syncguard.log:90"),
    }, [])


def decision(**overrides):
    """Arguments of clearing_action where every safety condition for unpause holds."""
    arguments = {"container_state": "paused", "terminal_outcome": "aborted", "other_signer_height": None,
                 "signer_check_ok": True, "backup_address": MOCK_ADDRESS, "validator": TARGET_ADDRESS}
    return {**arguments, **overrides}


def test_clearing_action_unpauses_only_when_every_condition_holds():
    """One failed condition, or any doubt, turns the unpause into a kill."""
    assert clearing_action(**decision())[0] == "unpause"
    assert clearing_action(**decision(container_state="running"))[0] == "none"
    assert clearing_action(**decision(container_state="unknown"))[0] == "kill"
    assert clearing_action(**decision(terminal_outcome="handed_over"))[0] == "kill"
    assert clearing_action(**decision(terminal_outcome=None))[0] == "kill"
    assert clearing_action(**decision(other_signer_height=1250))[0] == "kill"
    assert clearing_action(**decision(signer_check_ok=False))[0] == "kill"
    assert clearing_action(**decision(backup_address=UNREACHABLE))[0] == "kill"
    assert clearing_action(**decision(backup_address=TARGET_ADDRESS))[0] == "kill"


def test_restart_outcome_reports_both_markers_in_log_order():
    """A later success does not hide an earlier failure, and the reverse."""
    failed = log_event("restart_failed", 1000, "a:1")
    restarted = LogEvent("container_restarted", "validator-1", 2000, "info", "m", "a:2")
    assert restart_outcome(FakeProbes({}, []), 0) == ("not_attempted", None)
    assert restart_outcome(FakeProbes({("validator-1", "restart_failed"): failed}, []), 0) == ("failed", "a:1")
    both = FakeProbes({("validator-1", "restart_failed"): failed, ("validator-1", "container_restarted"): restarted}, [])
    assert restart_outcome(both, 0) == ("failed_then_succeeded", "a:1; a:2")
    later_failure = log_event("restart_failed", 3000, "a:3")
    reverse = FakeProbes({("validator-1", "restart_failed"): later_failure,
                          ("validator-1", "container_restarted"): restarted}, [])
    assert restart_outcome(reverse, 0) == ("succeeded_then_failed", "a:2; a:3")


def test_refusal_pauses_nothing(tmp_path):
    """Preconditions run before docker pause."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")]})
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run_hang(env, session, 1, HangSettings(), FakeProbes({}, []), FakeMonitor())
    assert list(PAUSE) not in env.commands.calls


def test_aborted_failover_with_no_other_signer_is_unpaused_and_settled(tmp_path):
    """Verified sole holder: unpause, check the state afterwards, keep the monitor running for the 60 s settle."""
    env = ready_env(tmp_path, extra_http=MOCK_ONLY_COMMITS, extra_script={
        PAUSE: [(0, "", "")], INSPECT: status("paused", "running"), UNPAUSE: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000  # preconditions sleep 6 s while checking chain progress
    monitor = FakeMonitor()
    result = run_hang(env, session, 1, HangSettings(), aborted_probes(pause_ms), monitor)
    assert (result["detected"], result["ttd_s"], result["restart_outcome"], result["action"]) == (True, 33.0, "failed", "unpause")
    assert (result["terminal_outcome"], result["clear_exit_code"], result["state_after"], result["clear_ok"]) == (
        "aborted", 0, "running", True)
    assert (result["signer_check_ok"], result["other_signer_height"], result["backup_address"]) == (True, None, MOCK_ADDRESS)
    assert result["outcome"] == "detected"
    assert (monitor.started, monitor.stopped) == (1, 1)
    assert 60.0 in env.clock.slept
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert kinds[:8] == ["iteration_start", "preconditions_passed", "fault_injected", "observed", "hang_decision",
                         "fault_cleared", "hang_cleared", "post_clear_settle"]
    assert kinds[-2:] == ["heights_collected", "iteration_end"]
    assert env.commands.calls.index(list(PAUSE)) < env.commands.calls.index(list(UNPAUSE))
    stored = read_json(session.directory / "hang-1.json")
    assert (stored["ttd_s"], stored["terminal_outcome"], stored["backup_address"]) == (33.0, "aborted", MOCK_ADDRESS)


def test_undetected_hang_with_backup_on_the_key_is_killed(tmp_path):
    """No detection and no terminal outcome: kill, and the state after the kill is recorded."""
    backup_status = [load_fixture("rpc/status_validator2.json"), load_fixture("rpc/status_validator1.json")]
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]},
                    extra_http={"http://localhost:22657/status": backup_status})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_hang(env, session, 1, HangSettings(), FakeProbes({}, []), FakeMonitor())
    assert (result["outcome"], result["detected"], result["action"]) == ("not_detected", False, "kill")
    assert (result["clear_exit_code"], result["state_after"], result["clear_ok"]) == (0, "exited", True)
    assert list(UNPAUSE) not in env.commands.calls
    cleared = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "fault_cleared"]
    assert cleared[0]["command"] == "docker kill validator1-node"


def test_detected_but_no_terminal_marker_is_killed_after_the_bound(tmp_path):
    """Detection without a terminal outcome waits 2 x stop_timeout + 30 s from detection, then kills."""
    env = ready_env(tmp_path, extra_http=MOCK_ONLY_COMMITS,
                    extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    probes = FakeProbes({("validator-1", "max_failures"): log_event("max_failures", pause_ms + 33000, "s:1")}, [])
    result = run_hang(env, session, 1, HangSettings(), probes, FakeMonitor())
    assert (result["action"], result["terminal_outcome"]) == ("kill", None)
    assert sum(seconds for seconds in env.clock.slept if seconds == 1.0) >= 130


def test_handover_is_killed(tmp_path):
    """A completed failover means the key moved; the paused process must not come back."""
    env = ready_env(tmp_path, extra_http=MOCK_ONLY_COMMITS,
                    extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    probes = FakeProbes({
        ("validator-1", "max_failures"): log_event("max_failures", pause_ms + 33000, "s:1"),
        ("validator-1", "failover_complete"): log_event("failover_complete", pause_ms + 70000, "s:2"),
    }, [])
    result = run_hang(env, session, 1, HangSettings(), probes, FakeMonitor())
    assert (result["action"], result["terminal_outcome"]) == ("kill", "handed_over")


def test_unreachable_backup_is_killed(tmp_path):
    """The backup's /status failing after the pause is doubt, so the hang ends with kill."""
    backup_status = [load_fixture("rpc/status_validator2.json"), Unreachable("backup down")]
    env = ready_env(tmp_path, extra_http={**MOCK_ONLY_COMMITS, "http://localhost:22657/status": backup_status},
                    extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    result = run_hang(env, session, 1, HangSettings(), aborted_probes(pause_ms), FakeMonitor())
    assert (result["action"], result["backup_address"]) == ("kill", UNREACHABLE)


def test_validator_signature_after_the_pause_is_killed(tmp_path):
    """A flag-2 precommit by the validator at a height after the pause means another process signed."""
    env = ready_env(tmp_path, extra_http={"http://localhost:26657/status": rising_status(1188, step=5)},
                    extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    result = run_hang(env, session, 1, HangSettings(), aborted_probes(pause_ms), FakeMonitor())
    assert result["action"] == "kill"
    assert result["other_signer_height"] is not None
    assert "another process" in result["reason"]


def test_pause_failure_with_a_running_container_injects_nothing(tmp_path):
    """A failed pause that left the container running: nothing to clear, no kill, outcome fault_command_failed."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(-1, "", "timeout")], INSPECT: status("running")})
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    result = run_hang(env, session, 1, HangSettings(), FakeProbes({}, []), monitor)
    assert (result["outcome"], result["fault_injected"], result["pause_exit_code"]) == ("fault_command_failed", False, -1)
    assert list(KILL) not in env.commands.calls and list(UNPAUSE) not in env.commands.calls
    assert (monitor.started, monitor.stopped) == (1, 1)


def test_pause_failure_with_unknown_state_is_killed(tmp_path):
    """If the container state cannot be read after a failed pause, kill is the only safe end."""
    env = ready_env(tmp_path, extra_script={
        PAUSE: [(-1, "", "timeout")], INSPECT: [(1, "", "daemon down"), (1, "", "daemon down"), (0, "exited\n", "")],
        KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_hang(env, session, 1, HangSettings(), FakeProbes({}, []), FakeMonitor())
    assert (result["action"], result["container_state"]) == ("kill", "unknown")
    assert env.commands.calls.count(list(KILL)) == 1
    assert result["state_after"] == "exited"


def test_pause_failure_with_a_paused_container_continues(tmp_path):
    """A pause command that reports failure but did pause is followed through like a successful pause."""
    env = ready_env(tmp_path, extra_http=MOCK_ONLY_COMMITS, extra_script={
        PAUSE: [(-1, "", "timeout")], INSPECT: status("paused", "paused", "running"), UNPAUSE: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    result = run_hang(env, session, 1, HangSettings(), aborted_probes(pause_ms), FakeMonitor())
    assert (result["action"], result["detected"], result["pause_exit_code"]) == ("unpause", True, -1)


def test_clear_failure_is_reported_explicitly(tmp_path):
    """A kill that leaves the container paused is a clear_failed outcome, not a quiet success."""
    env = ready_env(tmp_path, extra_script={
        PAUSE: [(0, "", "")], INSPECT: status("paused", "paused"), KILL: [(1, "", "kill failed")]})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_hang(env, session, 1, HangSettings(), FakeProbes({}, []), FakeMonitor())
    assert (result["outcome"], result["clear_exit_code"], result["state_after"], result["clear_ok"]) == (
        "clear_failed", 1, "paused", False)


def test_exception_after_the_pause_is_logged_and_the_monitor_stopped(tmp_path):
    """An error while waiting is recorded with its text, the monitor stops, the log window is kept, and the error re-raises."""
    class ExplodingProbes(FakeProbes):
        """Probes whose first marker wait fails."""

        def marker(self, node, marker, since_ms, timeout_s):
            """Fail like a broken log read."""
            raise OSError("log read failed")

    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(OSError):
        run_hang(env, session, 1, HangSettings(), ExplodingProbes({}, []), monitor)
    rows = read_events(session.directory / "events.jsonl")
    aborted = [row for row in rows if row["kind"] == "iteration_aborted"]
    assert len(aborted) == 1 and "log read failed" in aborted[0]["error"]
    assert (monitor.started, monitor.stopped) == (1, 1)
    assert not any(row["kind"] == "cleanup_failed" for row in rows)
