"""State, lock, and key-side snapshots; private keys never leave the file."""

import json

from fakes import NOT_A_KEY, FakeClock, FakeCommands, FakeHttp, make_session, paths_under, write_key, write_state
from harness.environment import build_environment
from harness.jsonio import read_jsonl
from harness.session import read_events
from harness.snapshots import key_file_fact, lock_file_fact, node_snapshot, state_fact, take_snapshot

TARGET = "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
MOCK = "E6FAA3F2B3E9158BA9BF93BE1F7F8D3095EAD04D"


def test_key_fact_keeps_address_and_hash_only(tmp_path):
    """The private value is not in the fact."""
    path = tmp_path / "priv_validator_key.json"
    write_key(path, TARGET)
    fact = key_file_fact(path)
    assert fact["exists"] is True
    assert fact["address"] == TARGET
    assert len(fact["sha256"]) == 64
    assert NOT_A_KEY not in json.dumps(fact)
    assert key_file_fact(tmp_path / "missing.json") == {"path": str(tmp_path / "missing.json"), "exists": False}


def test_lock_fact_reads_the_pid(tmp_path):
    """Content, PID, and whether that PID runs."""
    path = tmp_path / "priv_validator_state.json.lock"
    path.write_text("4242\n")
    fact = lock_file_fact(path, lambda pid: pid == 4242)
    assert (fact["content"], fact["pid"], fact["pid_alive"]) == ("4242", 4242, True)
    path.write_text("not-a-pid\n")
    assert lock_file_fact(path, lambda pid: True)["pid"] is None


def test_state_fact_drops_the_signature(tmp_path):
    """Height, round, step; no signature fields."""
    path = tmp_path / "priv_validator_state.json"
    write_state(path, 175)
    fact = state_fact(path)
    assert (fact["height"], fact["round"], fact["step"]) == (175, 0, 3)
    assert "signature" not in fact and "c2ln" not in json.dumps(fact)


def test_node_snapshot_lists_key_side_files_and_locks(tmp_path):
    """Key, .real, .staged (missing ones flagged), the state, and every lock."""
    key = tmp_path / "config" / "priv_validator_key.json"
    state = tmp_path / "data" / "priv_validator_state.json"
    write_key(key, MOCK)
    write_key(key.with_name("priv_validator_key.json.real"), TARGET)
    write_state(state, 175)
    state.with_name("priv_validator_state.json.lock").write_text("4242\n")
    snapshot = node_snapshot("validator2", key, state, lambda pid: False)
    assert [item["address"] if item["exists"] else None for item in snapshot["keys"]] == [MOCK, TARGET, None]
    assert [item["pid"] for item in snapshot["locks"]] == [4242]
    assert snapshot["state"]["height"] == 175
    assert NOT_A_KEY not in json.dumps(snapshot)


def test_take_snapshot_writes_both_nodes_and_an_event(tmp_path):
    """snapshots.jsonl gets one row with both nodes; events.jsonl gets a snapshot event."""
    paths = paths_under(tmp_path)
    write_key(paths.primary_key, TARGET)
    write_key(paths.backup_key, MOCK)
    write_state(paths.primary_state, 180)
    write_state(paths.backup_state, 175)
    paths.primary_state.with_name("priv_validator_state.json.lock").write_text("4242\n")
    clock = FakeClock(1791169200000)
    commands = FakeCommands({("ps", "-p", "4242", "-o", "pid="): [(0, " 4242\n", "")]}, clock)
    env = build_environment(FakeHttp(), commands, clock, paths, 25200)
    session = make_session(paths.data_dir, clock)
    snapshot = take_snapshot(env, session, 3, "before_fault")
    rows = read_jsonl(session.directory / "snapshots.jsonl")
    assert rows == [snapshot]
    assert (snapshot["session"], snapshot["iteration"], snapshot["point"]) == ("S4", 3, "before_fault")
    assert [node["node"] for node in snapshot["nodes"]] == ["validator1", "validator2"]
    assert snapshot["nodes"][0]["locks"][0]["pid_alive"] is True
    event = read_events(session.directory / "events.jsonl")[-1]
    assert (event["kind"], event["point"], event["iteration"]) == ("snapshot", "before_fault", 3)
