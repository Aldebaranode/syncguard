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


def finish_env(tmp_path):
    """Environment whose chain tip is 1188, with blocks 1183-1186 and copied smoke logs."""
    paths = paths_under(tmp_path)
    paths.active_log.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "logs" / "smoke-active.txt", paths.active_log)
    shutil.copy(FIXTURES / "logs" / "smoke-passive.txt", paths.passive_log)
    times = {height: f"2026-10-05T03:57:{height - 1150:02d}.000Z" for height in range(1183, 1187)}
    http = FakeHttp({
        "http://localhost:26657/status": load_fixture("rpc/status_rpc1.json"),
        "http://localhost:26657/consensus_params": load_fixture("rpc/consensus_params.json"),
        "http://localhost:26657/block?height=*": block_answers(times),
        SIGNING_INFO_URL: load_fixture("story/signing_info.json"),
        "http://localhost:9090/-/ready": Unreachable("prometheus down"),
    })
    clock = FakeClock(NOW)
    return build_environment(http, FakeCommands(clock=clock), clock, paths, 25200)


def test_collect_safety_scans_forward_from_the_first_fault(tmp_path):
    """Scan 1183..1186 (tip 1188 minus 2), signing info, monitor summary, and log copies."""
    env = finish_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    session.events.append("fault_injected", height=1185, iteration=2)
    session.events.append("fault_injected", height=1183, iteration=1)
    append_jsonl(session.directory / "monitor.jsonl", {"session": "S4", "t_ms": NOW, "addresses": {"validator1-node": "A", "validator2-node": "B"}})
    safety = collect_safety(env, session)
    assert (safety["evidence_scan"]["first_height"], safety["evidence_scan"]["last_height"]) == (1183, 1186)
    assert safety["evidence_scan"]["evidence"] == []
    assert safety["signing_info"]["missed_blocks_counter"] == 3
    assert safety["signing_info"]["tombstoned"] is False
    assert safety["prometheus"] == {"available": False}
    assert safety["monitor"]["samples"] == 1
    assert safety["monitor"]["dual_signer_seconds"] == 0
    assert safety["logs"] == ["logs/syncguard.log", "logs/syncguard-passive.log"]
    assert (session.directory / "logs" / "syncguard.log").read_bytes() == env.paths.active_log.read_bytes()
    assert read_json(session.directory / "safety.json")["session"] == "S4"
    assert read_events(session.directory / "events.jsonl")[-1]["kind"] == "finish"


def test_collect_safety_needs_a_fault(tmp_path):
    """Without a fault height there is no forward scan to make."""
    env = finish_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(ValueError, match="no fault injection"):
        collect_safety(env, session)
