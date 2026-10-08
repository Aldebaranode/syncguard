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
from harness.scenarios.hang import (
    RESULT_KEYS,
    HangSettings,
    clearing_action,
    other_signer_after,
    restart_outcome,
    run_hang,
    run_hang_series,
)
from harness.signing import HeightRecord
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
        run_hang(env, session, 1, HangSettings(), FakeProbes({}, []))
    assert list(PAUSE) not in env.commands.calls


def test_aborted_failover_with_no_other_signer_is_unpaused_and_settled(tmp_path):
    """Verified sole holder: unpause, check the state afterwards, settle 100 s before the next run."""
    env = ready_env(tmp_path, extra_http=MOCK_ONLY_COMMITS, extra_script={
        PAUSE: [(0, "", "")], INSPECT: status("paused", "running"), UNPAUSE: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000  # preconditions sleep 6 s while checking chain progress
    result = run_hang(env, session, 1, HangSettings(), aborted_probes(pause_ms))
    assert (result["detected"], result["t_fault_ms"], result["t_detect_ms"]) == (True, pause_ms, pause_ms + 33000)
    assert (result["restart_outcome"], result["action"]) == ("failed", "unpause")
    assert (result["terminal_outcome"], result["clear_exit_code"], result["state_after"], result["clear_ok"]) == (
        "aborted", 0, "running", True)
    assert (result["signer_check_ok"], result["other_signer_height"], result["backup_address"]) == (True, None, MOCK_ADDRESS)
    assert (result["outcome"], result["followup"]) == ("complete", "not_needed")
    assert 100.0 in env.clock.slept
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert kinds[:8] == ["preconditions_passed", "iteration_start", "fault_injected", "observed", "hang_decision",
                         "fault_cleared", "hang_cleared", "post_clear_settle"]
    assert kinds[-2:] == ["heights_collected", "iteration_end"]
    assert env.commands.calls.index(list(PAUSE)) < env.commands.calls.index(list(UNPAUSE))
    stored = read_json(session.directory / "hang-1.json")
    assert (stored["t_detect_ms"], stored["terminal_outcome"], stored["backup_address"]) == (
        pause_ms + 33000, "aborted", MOCK_ADDRESS)
    assert "ttd_s" not in stored


def test_undetected_hang_with_backup_on_the_key_is_killed(tmp_path):
    """No detection and no terminal outcome: kill, and the state after the kill is recorded."""
    backup_status = [load_fixture("rpc/status_validator2.json"), load_fixture("rpc/status_validator1.json")]
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]},
                    extra_http={"http://localhost:22657/status": backup_status})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_hang(env, session, 1, HangSettings(), FakeProbes({}, []))
    assert (result["outcome"], result["detected"], result["action"]) == ("not_detected", False, "kill")
    assert result["followup"] == "no_failover"
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
    result = run_hang(env, session, 1, HangSettings(), probes)
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
    result = run_hang(env, session, 1, HangSettings(), probes)
    assert (result["action"], result["terminal_outcome"]) == ("kill", "handed_over")
    assert (result["outcome"], result["followup"]) == ("no_takeover", "no_takeover")
    assert result["t_failover_complete_ms"] == pause_ms + 70000


def test_unreachable_backup_is_killed(tmp_path):
    """The backup's /status failing after the pause is doubt, so the hang ends with kill."""
    backup_status = [load_fixture("rpc/status_validator2.json"), Unreachable("backup down")]
    env = ready_env(tmp_path, extra_http={**MOCK_ONLY_COMMITS, "http://localhost:22657/status": backup_status},
                    extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    result = run_hang(env, session, 1, HangSettings(), aborted_probes(pause_ms))
    assert (result["action"], result["backup_address"]) == ("kill", UNREACHABLE)


def test_validator_signature_after_the_pause_is_killed(tmp_path):
    """A flag-2 precommit by the validator at a height after the pause means another process signed."""
    env = ready_env(tmp_path, extra_http={"http://localhost:26657/status": rising_status(1188, step=5)},
                    extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    result = run_hang(env, session, 1, HangSettings(), aborted_probes(pause_ms))
    assert result["action"] == "kill"
    assert result["other_signer_height"] is not None
    assert "another process" in result["reason"]


def test_pause_failure_with_a_running_container_injects_nothing(tmp_path):
    """A failed pause that left the container running: nothing to clear, no kill, outcome fault_command_failed."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(-1, "", "timeout")], INSPECT: status("running")})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_hang(env, session, 1, HangSettings(), FakeProbes({}, []))
    assert (result["outcome"], result["fault_injected"], result["pause_exit_code"]) == ("fault_command_failed", False, -1)
    assert list(KILL) not in env.commands.calls and list(UNPAUSE) not in env.commands.calls


def test_pause_failure_with_unknown_state_is_killed(tmp_path):
    """If the container state cannot be read after a failed pause, kill is the only safe end."""
    env = ready_env(tmp_path, extra_script={
        PAUSE: [(-1, "", "timeout")], INSPECT: [(1, "", "daemon down"), (1, "", "daemon down"), (0, "exited\n", "")],
        KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_hang(env, session, 1, HangSettings(), FakeProbes({}, []))
    assert (result["action"], result["container_state"]) == ("kill", "unknown")
    assert env.commands.calls.count(list(KILL)) == 1
    assert result["state_after"] == "exited"


def test_pause_failure_with_a_paused_container_continues(tmp_path):
    """A pause command that reports failure but did pause is followed through like a successful pause."""
    env = ready_env(tmp_path, extra_http=MOCK_ONLY_COMMITS, extra_script={
        PAUSE: [(-1, "", "timeout")], INSPECT: status("paused", "paused", "running"), UNPAUSE: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    result = run_hang(env, session, 1, HangSettings(), aborted_probes(pause_ms))
    assert (result["action"], result["detected"], result["pause_exit_code"]) == ("unpause", True, -1)


def test_clear_failure_is_reported_explicitly(tmp_path):
    """A kill that leaves the container paused is a clear_failed outcome, not a quiet success."""
    env = ready_env(tmp_path, extra_script={
        PAUSE: [(0, "", "")], INSPECT: status("paused", "paused"), KILL: [(1, "", "kill failed")]})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_hang(env, session, 1, HangSettings(), FakeProbes({}, []))
    assert (result["outcome"], result["clear_exit_code"], result["state_after"], result["clear_ok"]) == (
        "clear_failed", 1, "paused", False)


class ExplodingProbes(FakeProbes):
    """Probes whose marker wait raises the given exception."""

    def __init__(self, error: BaseException):
        """Keep the exception to raise."""
        super().__init__({}, [])
        self.error = error

    def marker(self, node, marker, since_ms, timeout_s):
        """Raise the configured exception."""
        raise self.error


def test_exception_after_the_pause_kills_the_paused_container(tmp_path):
    """An error between pause and clearing is logged, the paused primary is killed (never unpaused), and the error re-raises."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(OSError):
        run_hang(env, session, 1, HangSettings(), ExplodingProbes(OSError("log read failed")))
    rows = read_events(session.directory / "events.jsonl")
    aborted = [row for row in rows if row["kind"] == "iteration_aborted"]
    assert len(aborted) == 1 and "log read failed" in aborted[0]["error"]
    kill = [row for row in rows if row["kind"] == "abort_kill"]
    assert [(row["command"], row["exit_code"]) for row in kill] == [("docker kill validator1-node", 0)]
    assert [row["state_after"] for row in rows if row["kind"] == "abort_kill_result"] == ["exited"]
    assert list(UNPAUSE) not in env.commands.calls
    assert not any(row["kind"] == "cleanup_failed" for row in rows)


def test_interrupt_after_the_pause_kills_the_paused_container(tmp_path):
    """Ctrl-C between pause and clearing takes the same kill path and logs iteration_interrupted."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(KeyboardInterrupt):
        run_hang(env, session, 1, HangSettings(), ExplodingProbes(KeyboardInterrupt()))
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert "iteration_interrupted" in kinds and "abort_kill" in kinds


def test_abort_cleanup_leaves_an_already_exited_container_alone(tmp_path):
    """If the container is already exited the cleanup logs a skip and issues no kill."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: status("exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(OSError):
        run_hang(env, session, 1, HangSettings(), ExplodingProbes(OSError("x")))
    assert list(KILL) not in env.commands.calls
    assert any(row["kind"] == "abort_cleanup_skipped" for row in read_events(session.directory / "events.jsonl"))


def test_abort_before_the_pause_never_kills_a_healthy_primary(tmp_path):
    """An error before docker pause is issued must not turn into a kill of the running primary."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    real_tip = env.rpc.tip_height
    calls = []

    def tip_fails_after_preconditions():
        """The real tip for the preconditions and the first height, then an outage (the fault height read)."""
        calls.append(1)
        if len(calls) > 4:
            raise Unreachable("rpc down")
        return real_tip()

    env.rpc.tip_height = tip_fails_after_preconditions
    with pytest.raises(Unreachable):
        run_hang(env, session, 1, HangSettings(), FakeProbes({}, []))
    assert list(KILL) not in env.commands.calls


def test_not_injected_result_has_every_key_of_the_full_schema(tmp_path):
    """A failed pause on a running container still writes the full hang result schema, with None for unknowns."""
    ok = ready_env(tmp_path / "a", extra_http=MOCK_ONLY_COMMITS, extra_script={
        PAUSE: [(0, "", "")], INSPECT: status("paused", "running"), UNPAUSE: [(0, "", "")]})
    full = run_hang(ok, make_session(ok.paths.data_dir, ok.clock), 1, HangSettings(),
                    aborted_probes(ok.clock.now_ms() + 6000))
    failed = ready_env(tmp_path / "b", extra_script={PAUSE: [(-1, "", "timeout")], INSPECT: status("running")})
    result = run_hang(failed, make_session(failed.paths.data_dir, failed.clock), 1, HangSettings(),
                      FakeProbes({}, []))
    assert set(result) == set(full) == set(RESULT_KEYS)
    assert result["terminal_outcome"] is None and result["signer_check_ok"] is None and result["clear_ok"] is None


def test_validator_signature_in_the_tip_commit_counts(tmp_path):
    """A flag-2 entry at the newest height is a signature; the check reads through the tip."""
    def commit(url: str):
        """The validator signs only height 1200."""
        height = int(url.rsplit("=", 1)[1])
        return signed_commit_answer([TARGET_ADDRESS] if height == 1200 else [MOCK_ADDRESS])(url)

    env = ready_env(tmp_path, extra_http={"http://localhost:26657/status": {"result": {"sync_info": {"latest_block_height": "1200"}}},
                                          "http://localhost:26657/commit?height=*": commit})
    session = make_session(env.paths.data_dir, env.clock)
    assert other_signer_after(env, session, 1195, env.clock.now_ms()) == (True, 1200)


def test_unpause_becomes_kill_when_a_handover_marker_appears_just_before(tmp_path):
    """The logs are polled once more right before the unpause; a handover marker seen then forces a kill."""
    class LateHandoverProbes(FakeProbes):
        """Aborted failover; the backup's takeover_started shows up only on the last poll."""

        def marker(self, node, marker, since_ms, timeout_s):
            """Answer like aborted_probes, plus a late takeover_started from the second terminal-style poll on."""
            self.calls.append(("marker", node, marker))
            if marker == "takeover_started":
                late = sum(1 for call in self.calls if call[2] == "takeover_started") > 1
                return LogEvent("takeover_started", "validator-2", since_ms + 1, "info", "m", "passive.log:7") if late else None
            return self.markers.get((node, marker))

    env = ready_env(tmp_path, extra_http=MOCK_ONLY_COMMITS, extra_script={
        PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    base = aborted_probes(pause_ms)
    probes = LateHandoverProbes(base.markers, [])
    result = run_hang(env, session, 1, HangSettings(), probes)
    assert (result["action"], result["late_handover_source"]) == ("kill", "passive.log:7")
    assert list(UNPAUSE) not in env.commands.calls


def test_hang_json_is_written_before_final_heights_and_a_heights_error_is_logged(tmp_path):
    """A collection error after the result was written keeps hang-<n>.json and logs iteration_aborted."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    working = env.http.answers["http://localhost:26657/commit?height=*"]

    def commits_fail_late(url):
        """Normal commits until the hang is cleared, then the rpc is down."""
        cleared = any(row["kind"] == "hang_cleared" for row in read_events(session.directory / "events.jsonl"))
        return Unreachable("rpc down") if cleared else working(url)

    env.http.answers["http://localhost:26657/commit?height=*"] = commits_fail_late
    with pytest.raises(Unreachable):
        run_hang(env, session, 1, HangSettings(), FakeProbes({}, []))
    assert read_json(session.directory / "hang-1.json")["action"] == "kill"
    assert any(row["kind"] == "iteration_aborted" for row in read_events(session.directory / "events.jsonl"))


def interrupting_commands(env, interrupts: dict):
    """Make env.commands.run raise KeyboardInterrupt the listed number of times for each docker action word."""
    original = env.commands.run
    remaining = dict(interrupts)

    def run(argv, timeout=120.0):
        """Raise the scheduled Ctrl-C for this action, otherwise run the scripted command."""
        action = argv[1] if len(argv) > 1 else ""
        if remaining.get(action, 0) > 0:
            remaining[action] -= 1
            raise KeyboardInterrupt()
        return original(argv, timeout)

    env.commands.run = run


def test_abort_cleanup_survives_a_second_interrupt_and_retries_the_kill(tmp_path):
    """Ctrl-C during the cleanup's inspect and first kill is logged; the retry kills; the original error re-raises."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: status("exited"), KILL: [(0, "", "")]})
    interrupting_commands(env, {"inspect": 1, "kill": 1})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(OSError, match="first failure"):
        run_hang(env, session, 1, HangSettings(), ExplodingProbes(OSError("first failure")))
    rows = read_events(session.directory / "events.jsonl")
    steps = [row["step"] for row in rows if row["kind"] == "cleanup_failed"]
    assert steps == ["inspect", "kill attempt 1"]
    kills = [row for row in rows if row["kind"] == "abort_kill"]
    assert [(row["attempt"], row["exit_code"]) for row in kills] == [(2, 0)]


def test_abort_cleanup_reports_a_kill_that_never_completes(tmp_path):
    """Two failed kill attempts log cleanup_failed with the error text; the original error still re-raises."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused"), KILL: [(1, "", "denied")]})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(OSError):
        run_hang(env, session, 1, HangSettings(), ExplodingProbes(OSError("x")))
    rows = read_events(session.directory / "events.jsonl")
    assert [row["attempt"] for row in rows if row["kind"] == "abort_kill"] == [1, 2]
    failed = [row for row in rows if row["kind"] == "cleanup_failed" and row["step"] == "kill"]
    assert len(failed) == 1 and "did not complete" in failed[0]["error"]


def test_failed_unpause_is_followed_by_a_kill(tmp_path):
    """An unpause that leaves the container paused is followed by docker kill, recorded in the result."""
    env = ready_env(tmp_path, extra_http=MOCK_ONLY_COMMITS, extra_script={
        PAUSE: [(0, "", "")], INSPECT: status("paused", "paused", "exited"), UNPAUSE: [(1, "", "failed")],
        KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_hang(env, session, 1, HangSettings(), aborted_probes(env.clock.now_ms() + 6000))
    assert (result["action"], result["clear_exit_code"], result["followup_kill_exit_code"]) == ("unpause", 1, 0)
    assert (result["state_after"], result["clear_ok"]) == ("exited", True)
    assert env.commands.calls.index(list(UNPAUSE)) < env.commands.calls.index(list(KILL))
    assert any(row["kind"] == "fault_cleared_after_failed_unpause"
               for row in read_events(session.directory / "events.jsonl"))


def recovery_markers(pause_ms: int) -> dict:
    """Probe answers of a full recovery after the hang: failover complete, takeover, failback, and release."""
    return {
        ("validator-1", "failover_complete"): log_event("failover_complete", pause_ms + 120_000, "syncguard.log:95"),
        ("validator-2", "takeover_complete"): LogEvent("takeover_complete", "validator-2", pause_ms + 125_000, "info", "m",
                                                       "syncguard-passive.log:40"),
        ("validator-1", "failback_complete"): log_event("failback_complete", pause_ms + 260_000, "syncguard.log:120"),
        ("validator-2", "release_complete"): LogEvent("release_complete", "validator-2", pause_ms + 258_000, "info", "m",
                                                      "syncguard-passive.log:60"),
    }


def recovery_signatures(pause_ms: int) -> list:
    """The backup's first signature after the takeover and the primary's first signature after the release."""
    return [HeightRecord(1240, pause_ms + 130_000, "P", 0, {TARGET_ADDRESS: pause_ms + 130_000}, {}, 0),
            HeightRecord(1290, pause_ms + 265_000, "P", 0, {TARGET_ADDRESS: pause_ms + 265_000}, {}, 0)]


def test_a_handover_is_followed_through_failback_and_settled_with_its_times_recorded(tmp_path):
    """After the kill of a handed-over hang the runner waits for takeover, both first signatures, failback, and the
    release, then settles 100 s, so the next run's preconditions can pass."""
    env = ready_env(tmp_path, extra_http=MOCK_ONLY_COMMITS,
                    extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    markers = {("validator-1", "max_failures"): log_event("max_failures", pause_ms + 33000, "s:1"),
               **recovery_markers(pause_ms)}
    result = run_hang(env, session, 1, HangSettings(), FakeProbes(markers, recovery_signatures(pause_ms)))
    assert (result["action"], result["terminal_outcome"]) == ("kill", "handed_over")
    assert (result["outcome"], result["followup"]) == ("complete", "complete")
    assert (result["t_takeover_ms"], result["backup_signature_height"], result["t_backup_signature_ms"]) == (
        pause_ms + 125_000, 1240, pause_ms + 130_000)
    assert (result["t_failback_complete_ms"], result["t_release_complete_ms"]) == (pause_ms + 260_000, pause_ms + 258_000)
    assert (result["primary_signature_height"], result["t_primary_signature_ms"]) == (1290, pause_ms + 265_000)
    assert result["followup_sources"]["takeover"] == "syncguard-passive.log:40"
    rows = read_events(session.directory / "events.jsonl")
    waits = [row["waiting_for"] for row in rows if row["kind"] == "observed"]
    assert waits[-6:] == ["failover_complete", "takeover_complete", "backup_first_signature", "failback_complete",
                          "release_complete", "primary_first_signature"]
    settle = [row for row in rows if row["kind"] == "post_clear_settle"]
    assert settle[0]["seconds"] == 100.0
    assert [row["kind"] for row in rows].index("post_clear_settle") > [row["kind"] for row in rows].index("hang_cleared")
    assert read_json(session.directory / "hang-1.json")["followup"] == "complete"


class AfterClearProbes(FakeProbes):
    """FakeProbes whose recovery markers appear only once the session logged hang_cleared (the kill provokes them)."""

    def __init__(self, session, markers: dict, signatures: list, recovery_names: set):
        """Keep the session to watch, and the (node, marker) names that only exist after the clearing action."""
        super().__init__(markers, signatures)
        self.session = session
        self.recovery_names = recovery_names

    def marker(self, node, marker, since_ms, timeout_s):
        """The scripted event, hidden for recovery markers until the hang was cleared."""
        cleared = any(row["kind"] == "hang_cleared" for row in read_events(self.session.directory / "events.jsonl"))
        if (node, marker) in self.recovery_names and not cleared:
            self.calls.append(("marker", node, marker))
            return None
        return super().marker(node, marker, since_ms, timeout_s)


def test_a_clearing_kill_after_an_aborted_failover_is_followed_like_a_crash(tmp_path):
    """syncguard aborted, the harness killed (backup unreachable), and the failover the kill provokes is followed."""
    backup_status = [load_fixture("rpc/status_validator2.json"), Unreachable("backup down")]
    env = ready_env(tmp_path, extra_http={**MOCK_ONLY_COMMITS, "http://localhost:22657/status": backup_status},
                    extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000
    recovery = recovery_markers(pause_ms)
    probes = AfterClearProbes(session, {**aborted_probes(pause_ms).markers, **recovery},
                              recovery_signatures(pause_ms), set(recovery))
    result = run_hang(env, session, 1, HangSettings(), probes)
    assert (result["terminal_outcome"], result["action"], result["followup"], result["outcome"]) == (
        "aborted", "kill", "complete", "complete")


def test_the_hang_result_holds_instants_and_no_derived_seconds():
    """TTD and TTR are computed by the analysis with the clock uncertainty, not by the runner."""
    assert "ttd_s" not in RESULT_KEYS and "ttr_s" not in RESULT_KEYS
    assert {"t_fault_ms", "t_detect_ms", "followup", "t_takeover_ms", "t_primary_signature_ms"} <= set(RESULT_KEYS)


def test_hang_series_runs_one_monitor_and_numbers_its_iterations(tmp_path):
    """Two complete hang runs under one monitor start and stop, numbered one after the other."""
    env = ready_env(tmp_path, extra_script={
        PAUSE: [(0, "", "")], INSPECT: status("paused", "running", "paused", "running"), UNPAUSE: [(0, "", "")]})

    def signs_unless_paused(url):
        """The validator signs every commit except while more pauses than unpauses were issued."""
        paused = env.commands.calls.count(list(PAUSE)) > env.commands.calls.count(list(UNPAUSE))
        return signed_commit_answer([MOCK_ADDRESS] if paused else [TARGET_ADDRESS, MOCK_ADDRESS])(url)

    env.http.answers["http://localhost:26657/commit?height=*"] = signs_unless_paused
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    results = run_hang_series(env, session, 2, HangSettings(), aborted_probes(env.clock.now_ms() + 6000), monitor)
    assert [result["outcome"] for result in results] == ["complete", "complete"]
    assert (monitor.started, monitor.stopped) == (1, 1)
    assert (session.directory / "hang-1.json").exists() and (session.directory / "hang-2.json").exists()


def test_hang_series_stops_at_the_first_outcome_other_than_complete(tmp_path):
    """An undetected hang stops the series; series_aborted names the scenario and the outcome."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: status("paused", "exited"), KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    results = run_hang_series(env, session, 3, HangSettings(), FakeProbes({}, []), monitor)
    assert [result["outcome"] for result in results] == ["not_detected"]
    stops = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "series_aborted"]
    assert [(row["scenario"], row["iteration"], row["outcome"]) for row in stops] == [("hang", 1, "not_detected")]
    assert (monitor.started, monitor.stopped) == (1, 1)


def test_hang_series_stops_the_monitor_when_a_run_is_refused(tmp_path):
    """A refusal raises out of the series and the monitor still stops."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")]})
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(PreconditionError):
        run_hang_series(env, session, 2, HangSettings(), FakeProbes({}, []), monitor)
    assert (monitor.started, monitor.stopped) == (1, 1)
