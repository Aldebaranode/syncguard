"""Session-end safety collection."""

import shutil

import pytest

from fakes import FIXTURES, FakeClock, FakeCommands, FakeHttp, block_answers, load_fixture, make_session, paths_under
from harness.adapters import Unreachable
from harness.environment import build_environment
from harness.finish import collect_safety
from harness.jsonio import append_jsonl, read_json
from harness.session import read_events

NOW = 1791169200000
SIGNING_INFO_URL = ("http://localhost:1317/slashing/signing_infos/"
                    "0x02f2e8fa5920d145a8ce823ea49cefd5aeeea6df8768cc5909d5a0471bd9f026f5")


def finish_env(tmp_path, now_ms: int = NOW):
    """Environment whose chain tip is 1188, with blocks 1183-1188 and copied smoke logs, at now_ms."""
    paths = paths_under(tmp_path)
    paths.active_log.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "logs" / "smoke-active.txt", paths.active_log)
    shutil.copy(FIXTURES / "logs" / "smoke-passive.txt", paths.passive_log)
    times = {height: f"2026-10-05T03:57:{height - 1150:02d}.000Z" for height in range(1183, 1189)}
    http = FakeHttp({
        "http://localhost:26657/status": load_fixture("rpc/status_rpc1.json"),
        "http://localhost:26657/consensus_params": load_fixture("rpc/consensus_params.json"),
        "http://localhost:26657/block?height=*": block_answers(times),
        SIGNING_INFO_URL: load_fixture("story/signing_info.json"),
        "http://localhost:9090/-/ready": Unreachable("prometheus down"),
    })
    clock = FakeClock(now_ms)
    return build_environment(http, FakeCommands(clock=clock), clock, paths, 25200)


def test_collect_safety_scans_forward_from_the_first_fault(tmp_path):
    """Scan 1183..1188 (to the tip), signing info, monitor summary, and log copies."""
    env = finish_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    session.events.append("fault_injected", height=1185, iteration=2)
    session.events.append("fault_injected", height=1183, iteration=1)
    append_jsonl(session.directory / "monitor.jsonl", {"session": "S4", "t_ms": NOW, "addresses": {"validator1-node": "A", "validator2-node": "B"}})
    safety = collect_safety(env, session)
    assert (safety["evidence_scan"]["first_height"], safety["evidence_scan"]["last_height"]) == (1183, 1188)
    assert safety["evidence_scan"]["evidence"] == []
    assert safety["signing_info"]["missed_blocks_counter"] == 3
    assert safety["signing_info"]["tombstoned"] is False
    assert safety["prometheus"] == {"available": False}
    assert safety["monitor"]["samples"] == 1
    assert safety["monitor"]["dual_signer_seconds"] == 0
    assert safety["monitor"]["observed"] is True
    assert safety["logs"] == ["logs/syncguard.log", "logs/syncguard-passive.log"]
    assert (session.directory / "logs" / "syncguard.log").read_bytes() == env.paths.active_log.read_bytes()
    assert read_json(session.directory / "safety.json")["session"] == "S4"
    assert read_events(session.directory / "events.jsonl")[-1]["kind"] == "finish"


def test_a_session_without_faults_gets_a_logs_only_finish(tmp_path):
    """No fault means no forward scan and no safety.json, but the logs are copied for the session-wide checks."""
    env = finish_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    result = collect_safety(env, session)
    assert result == {"session": "S4", "logs_only": True, "logs": ["logs/syncguard.log", "logs/syncguard-passive.log"]}
    assert not (session.directory / "safety.json").exists()
    assert (session.directory / "logs" / "syncguard.log").read_bytes() == env.paths.active_log.read_bytes()
    last = read_events(session.directory / "events.jsonl")[-1]
    assert last["kind"] == "finish" and last["logs_only"] is True


def test_logs_are_copied_before_the_fault_check(tmp_path):
    """A fault event without a height still leaves the copied logs behind when the scan is refused."""
    env = finish_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    session.events.append("fault_injected", iteration=1)
    with pytest.raises(ValueError, match="no height"):
        collect_safety(env, session)
    assert (session.directory / "logs" / "syncguard.log").exists()


def test_missing_monitor_file_is_not_observed(tmp_path):
    """No monitor.jsonl means the dual-signer count was never measured, not that it was 0."""
    env = finish_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    session.events.append("fault_injected", height=1183, iteration=1, scenario="hang")
    monitor = collect_safety(env, session)["monitor"]
    assert monitor["observed"] is False
    assert monitor["dual_signer_seconds"] is None
    assert monitor["observed_problems"] == [{"code": "no_samples", "scenario": "hang", "iteration": 1}]


A = {"validator1-node": "48DC218393FCEEF56A37D963B804FAB92C62CA9D", "validator2-node": "MOCK"}


def write_samples(session, start_ms: int, end_ms: int, skip=lambda t_ms: False) -> None:
    """monitor.jsonl rows once per second from start_ms to end_ms, leaving out the instants skip() names."""
    for t_ms in range(start_ms, end_ms + 1, 1000):
        if not skip(t_ms):
            append_jsonl(session.directory / "monitor.jsonl", {"session": session.id, "t_ms": t_ms, "addresses": A})


def failover_run(session, iteration: int, start_ms: int, end_ms: int) -> None:
    """events of one followed failover iteration from start_ms to end_ms."""
    session.events.append("iteration_start", t_ms=start_ms, scenario="failover", iteration=iteration)
    session.events.append("fault_injected", t_ms=start_ms + 2000, scenario="failover", iteration=iteration,
                          height=1183, exit_code=0)
    session.events.append("iteration_end", t_ms=end_ms, scenario="failover", iteration=iteration)


@pytest.mark.parametrize("delay_s", [2, 6, 20, 60])
def test_a_series_monitored_to_its_end_is_observed_whenever_finish_runs(tmp_path, delay_s):
    """The probe case: a monitor that ran to the series end is observed with finish 2, 6, 20, or 60 s later."""
    start = NOW - 3_600_000
    env = finish_env(tmp_path, now_ms=start + 3_600_000 + delay_s * 1000)
    session = make_session(env.paths.data_dir, FakeClock(start - 10_000))
    for iteration in range(1, 11):
        failover_run(session, iteration, start + (iteration - 1) * 360_000, start + iteration * 360_000 - 1000)
    write_samples(session, start - 5000, start + 3_600_000)
    monitor = collect_safety(env, session)["monitor"]
    assert (monitor["observed"], monitor["observed_problems"]) == (True, [])
    assert len(monitor["windows"]) == 10


def test_three_hang_runs_six_minutes_apart_are_observed(tmp_path):
    """Each run has its own monitor; the minutes between runs need no samples."""
    start = NOW - 1_000_000
    env = finish_env(tmp_path, now_ms=start + 2 * 360_000 + 242_000)
    session = make_session(env.paths.data_dir, FakeClock(start - 10_000))
    for run in range(3):
        fault = start + run * 360_000
        session.events.append("fault_injected", t_ms=fault, scenario="hang", iteration=run + 1, height=1183,
                              exit_code=0)
        session.events.append("iteration_end", t_ms=fault + 240_000, scenario="hang", iteration=run + 1)
        write_samples(session, fault - 2000, fault + 240_000)
    monitor = collect_safety(env, session)["monitor"]
    assert (monitor["observed"], monitor["observed_problems"]) == (True, [])
    assert [window["iteration"] for window in monitor["windows"]] == [1, 2, 3]


def test_a_run_with_a_30_second_hole_is_not_observed(tmp_path):
    """The hole is named with the run it belongs to; the other run stays observed."""
    start = NOW - 1_000_000
    env = finish_env(tmp_path)
    session = make_session(env.paths.data_dir, FakeClock(start - 10_000))
    failover_run(session, 1, start, start + 200_000)
    failover_run(session, 2, start + 300_000, start + 500_000)
    write_samples(session, start - 2000, start + 500_000,
                  skip=lambda t_ms: start + 350_000 < t_ms < start + 380_000)
    monitor = collect_safety(env, session)["monitor"]
    assert monitor["observed"] is False
    assert monitor["observed_problems"] == [{"code": "gap", "ms": 30_000, "scenario": "failover", "iteration": 2}]
    assert [window["observed"] for window in monitor["windows"]] == [True, False]


def test_first_fault_without_a_height_is_refused(tmp_path):
    """The earliest fault_injected event must say where the scan starts."""
    env = finish_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    session.events.append("fault_injected", iteration=1)
    session.events.append("fault_injected", height=1185, iteration=2)
    with pytest.raises(ValueError, match="no height"):
        collect_safety(env, session)
