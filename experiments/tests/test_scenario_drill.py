"""baseline_drill: operator-in-the-loop manual recovery."""

import pytest

from fakes import PS_OUTPUT, TARGET_ADDRESS, FakeMonitor, FakeProbes, make_session, ready_env
from harness.adapters import Unreachable
from harness.jsonio import read_json
from harness.scenarios.common import PreconditionError
from harness.scenarios.drill import DrillSettings, run_drill
from harness.session import read_events
from harness.signing import HeightRecord

KILL = ("docker", "kill", "validator1-node")
RESTART = ("/bin/sh", "-c", "docker restart validator2-node")
PEERS_DOWN = {"http://localhost:8081/health": Unreachable("connection refused"),
              "http://localhost:8090/health": Unreachable("connection refused")}


def scripted(answers):
    """An ask() that returns the scripted answers in order."""
    remaining = iter(answers)

    def ask(prompt):
        """The next scripted answer."""
        return next(remaining)

    return ask


def test_refuses_without_an_operator(tmp_path):
    """A drill without a named operator never kills anything."""
    env = ready_env(tmp_path, ps_output="", extra_script={KILL: [(0, "", "")]}, extra_http=PEERS_DOWN)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError, match="human operator"):
        run_drill(env, session, DrillSettings(operator=" "), FakeProbes({}, []), FakeMonitor(), scripted([]), print)
    assert list(KILL) not in env.commands.calls


def test_refuses_while_syncguard_runs(tmp_path):
    """The baseline is recovery without syncguard."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError, match="syncguard is running"):
        run_drill(env, session, DrillSettings(operator="Heed"), FakeProbes({}, []), FakeMonitor(), scripted(["ready"]), print)
    assert list(KILL) not in env.commands.calls


def test_refuses_when_the_operator_does_not_confirm(tmp_path):
    """Anything but 'ready' stops before the kill."""
    env = ready_env(tmp_path, ps_output="", extra_script={KILL: [(0, "", "")]}, extra_http=PEERS_DOWN)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run_drill(env, session, DrillSettings(operator="Heed"), FakeProbes({}, []), FakeMonitor(), scripted(["no"]), print)
    assert list(KILL) not in env.commands.calls


def test_drill_records_alert_commands_and_first_signature(tmp_path):
    """Kill, alert, operator start, one command, first signature 300 s after the kill."""
    env = ready_env(tmp_path, ps_output="", extra_script={KILL: [(0, "", "")], RESTART: [(0, "validator2-node\n", "")]},
                    extra_http=PEERS_DOWN)
    session = make_session(env.paths.data_dir, env.clock)
    kill_ms = env.clock.now_ms() + 6000  # preconditions wait 6 s for chain progress
    probes = FakeProbes({}, [HeightRecord(1300, kill_ms + 300000, "P", 0, {TARGET_ADDRESS: kill_ms + 300000}, {}, 0)],
                        misses=[HeightRecord(1193, kill_ms + 4000, "P", 0, {}, {}, 1)])
    said = []
    monitor = FakeMonitor()
    result = run_drill(env, session, DrillSettings(operator="Heed"), probes, monitor,
                       scripted(["ready", "", "docker restart validator2-node", "", "done"]), said.append)
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert kinds == ["iteration_start", "preconditions_passed", "preconditions_passed", "fault_injected", "observed", "operator_start",
                     "operator_command", "observed", "post_recovery_monitoring", "heights_collected", "iteration_end"]
    assert (result["outcome"], result["recovery_s"], result["first_signature_height"]) == ("recovered", 300.0, 1300)
    assert result["commands"][0]["command"] == "docker restart validator2-node"
    assert result["alert_height"] == 1193
    assert any("ALERT" in line for line in said)
    assert read_json(session.directory / "drill.json")["operator"] == "Heed"
    assert (monitor.started, monitor.stopped) == (1, 1)
    assert 60.0 in env.clock.slept
    assert (result["alert_observed"], result["alert_s"]) == (True, 0.0)  # the fake clock does not advance
    assert probes.calls[-1] == ("signature", kill_ms + 4001)
    assert "kill command was issued" in result["recovery_definition"]


def test_refuses_while_a_peer_server_answers_health(tmp_path):
    """No syncguard process in ps is not enough; an answering /health also blocks the kill."""
    env = ready_env(tmp_path, ps_output="", extra_script={KILL: [(0, "", "")]},
                    extra_http={"http://localhost:8090/health": Unreachable("down")})
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError, match="health"):
        run_drill(env, session, DrillSettings(operator="Heed"), FakeProbes({}, []), FakeMonitor(), scripted(["ready"]), print)
    assert list(KILL) not in env.commands.calls


def test_no_alert_path_is_recorded_as_not_observed(tmp_path):
    """No missed height seen: alert_observed is false and the alert instants stay empty."""
    env = ready_env(tmp_path, ps_output="", extra_script={KILL: [(0, "", "")]}, extra_http=PEERS_DOWN)
    session = make_session(env.paths.data_dir, env.clock)
    kill_ms = env.clock.now_ms() + 6000
    probes = FakeProbes({}, [HeightRecord(1300, kill_ms + 300000, "P", 0, {TARGET_ADDRESS: kill_ms + 300000}, {}, 0)])
    said = []
    result = run_drill(env, session, DrillSettings(operator="Heed"), probes, FakeMonitor(),
                       scripted(["ready", "", "done"]), said.append)
    assert (result["alert_observed"], result["alert_height"], result["t_alert_ms"], result["alert_s"],
            result["reaction_s"]) == (False, None, None, None, None)
    assert result["recovery_s"] == 300.0
    assert probes.calls[-1] == ("signature", kill_ms)
    assert any("not observed" in line for line in said)
    waits = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "wait_timeout"]
    assert [row["waiting_for"] for row in waits] == ["alert_missed_height"]


def test_checks_run_again_after_the_operator_confirms(tmp_path):
    """A syncguard process that appears while the operator reads the runbook stops the drill before the kill."""
    env = ready_env(tmp_path, ps_output="", extra_script={
        KILL: [(0, "", "")], ("ps", "-axo", "pid=,command="): [(0, "", ""), (0, PS_OUTPUT, "")]}, extra_http=PEERS_DOWN)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(PreconditionError, match="syncguard is running"):
        run_drill(env, session, DrillSettings(operator="Heed"), FakeProbes({}, []), monitor, scripted(["ready"]), print)
    assert list(KILL) not in env.commands.calls
    assert monitor.started == 0


def test_no_signature_skips_the_post_recovery_period(tmp_path):
    """Without a recovery there is nothing to confirm; the monitor still stops."""
    env = ready_env(tmp_path, ps_output="", extra_script={KILL: [(0, "", "")]}, extra_http=PEERS_DOWN)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    result = run_drill(env, session, DrillSettings(operator="Heed"), FakeProbes({}, []), monitor,
                       scripted(["ready", "", "done"]), print)
    assert result["outcome"] == "no_signature"
    assert 60.0 not in env.clock.slept
    assert (monitor.started, monitor.stopped) == (1, 1)


def test_exception_after_the_kill_is_logged_and_the_monitor_stopped(tmp_path):
    """An error while the operator works is logged with its text, the monitor stops, and the error re-raises."""
    env = ready_env(tmp_path, ps_output="", extra_script={KILL: [(0, "", "")]}, extra_http=PEERS_DOWN)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()

    def failing_ask(prompt):
        """Confirm the start, then fail on the next prompt."""
        if "kill" in prompt:
            return "ready"
        raise EOFError("terminal closed")

    with pytest.raises(EOFError):
        run_drill(env, session, DrillSettings(operator="Heed"), FakeProbes({}, []), monitor, failing_ask, print)
    aborted = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "iteration_aborted"]
    assert len(aborted) == 1 and "terminal closed" in aborted[0]["error"]
    assert (monitor.started, monitor.stopped) == (1, 1)
