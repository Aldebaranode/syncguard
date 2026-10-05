"""hang runner: docker pause, detection, and a safe end of the hang."""

import pytest

from fakes import MOCK_ADDRESS, TARGET_ADDRESS, FakeMonitor, FakeProbes, load_fixture, make_session, ready_env, write_key
from harness.jsonio import read_json
from harness.logparse import LogEvent
from harness.monitor import UNREACHABLE
from harness.scenarios.common import PreconditionError
from harness.scenarios.hang import HangSettings, clearing_action, run_hang
from harness.session import read_events

PAUSE = ("docker", "pause", "validator1-node")
UNPAUSE = ("docker", "unpause", "validator1-node")
KILL = ("docker", "kill", "validator1-node")
INSPECT = ("docker", "inspect", "-f", "{{.State.Status}}", "validator1-node")


def test_clearing_action_rules():
    """Unpause only when the paused process would be the sole holder of the validator key."""
    assert clearing_action("running", False, MOCK_ADDRESS, TARGET_ADDRESS)[0] == "none"
    assert clearing_action("paused", True, MOCK_ADDRESS, TARGET_ADDRESS)[0] == "kill"
    assert clearing_action("paused", False, TARGET_ADDRESS, TARGET_ADDRESS)[0] == "kill"
    assert clearing_action("paused", False, MOCK_ADDRESS, TARGET_ADDRESS)[0] == "unpause"
    assert clearing_action("paused", False, UNREACHABLE, TARGET_ADDRESS)[0] == "unpause"


def test_refusal_pauses_nothing(tmp_path):
    """Preconditions run before docker pause."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")]})
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run_hang(env, session, 1, HangSettings(), FakeProbes({}, []), FakeMonitor())
    assert list(PAUSE) not in env.commands.calls


def test_detected_hang_with_failed_restart_is_unpaused(tmp_path):
    """Detection after 33 s, restart failed on the paused container, backup on a mock key: unpause."""
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: [(0, "paused\n", "")], UNPAUSE: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    pause_ms = env.clock.now_ms() + 6000  # preconditions sleep 6 s while checking chain progress
    probes = FakeProbes({
        ("validator-1", "max_failures"): LogEvent("max_failures", "validator-1", pause_ms + 33000, "error", "m", "syncguard.log:80"),
        ("validator-1", "restart_failed"): LogEvent("restart_failed", "validator-1", pause_ms + 90000, "error", "m", "syncguard.log:85"),
    }, [])
    monitor = FakeMonitor()
    result = run_hang(env, session, 1, HangSettings(), probes, monitor)
    assert (result["detected"], result["ttd_s"], result["restart_outcome"], result["action"]) == (True, 33.0, "failed", "unpause")
    assert (monitor.started, monitor.stopped) == (1, 1)
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert kinds == ["iteration_start", "preconditions_passed", "fault_injected", "observed", "hang_decision",
                     "fault_cleared", "heights_collected", "iteration_end"]
    assert env.commands.calls.index(list(PAUSE)) < env.commands.calls.index(list(UNPAUSE))
    assert read_json(session.directory / "hang-1.json")["ttd_s"] == 33.0
    assert 90.0 in env.clock.slept


def test_undetected_hang_with_backup_on_the_key_is_killed(tmp_path):
    """No detection within 60 s and the backup now runs the validator key: kill, never unpause."""
    backup_status = [load_fixture("rpc/status_validator2.json"), load_fixture("rpc/status_validator1.json")]
    env = ready_env(tmp_path, extra_script={PAUSE: [(0, "", "")], INSPECT: [(0, "paused\n", "")], KILL: [(0, "", "")]},
                    extra_http={"http://localhost:22657/status": backup_status})
    session = make_session(env.paths.data_dir, env.clock)
    result = run_hang(env, session, 1, HangSettings(), FakeProbes({}, []), FakeMonitor())
    assert (result["outcome"], result["detected"], result["action"]) == ("not_detected", False, "kill")
    assert list(UNPAUSE) not in env.commands.calls
    cleared = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "fault_cleared"]
    assert cleared[0]["command"] == "docker kill validator1-node"
