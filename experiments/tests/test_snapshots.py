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


CAT_STATE = ("cat", "priv_validator_state.json")
LS_LOCK = ("ls", "-l", "priv_validator_state.json.lock")
STATE_OUTPUT = '{\n  "height": "180",\n  "round": 0,\n  "step": 3,\n  "signature": "c2ln",\n  "signbytes": "AA"\n}'
LOCK_LISTING = "-rw-------  1 heed  staff  5 Oct  9 12:01 priv_validator_state.json.lock\n"
NO_LOCK = "ls: priv_validator_state.json.lock: No such file or directory\n"


def snapshot_env(tmp_path):
    """Both nodes' key and state files, a lock on validator1 only, and a runner scripted for the snapshot commands.

    The ls of the lock answers in call order: validator1 lists its lock, validator2 has none.
    """
    paths = paths_under(tmp_path)
    write_key(paths.primary_key, TARGET)
    write_key(paths.backup_key, MOCK)
    write_state(paths.primary_state, 180)
    write_state(paths.backup_state, 175)
    paths.primary_state.with_name("priv_validator_state.json.lock").write_text("4242\n")
    clock = FakeClock(1791169200000)
    commands = FakeCommands({
        ("ps", "-p", "4242", "-o", "pid="): [(0, " 4242\n", "")],
        CAT_STATE: [(0, STATE_OUTPUT, "")],
        LS_LOCK: [(0, LOCK_LISTING, ""), (1, "", NO_LOCK)],
    }, clock)
    return build_environment(FakeHttp(), commands, clock, paths, 25200)


def test_take_snapshot_runs_the_state_commands_in_each_nodes_state_directory(tmp_path):
    """cat of the state file and ls of its lock, both with the node's state directory as working directory."""
    env = snapshot_env(tmp_path)
    take_snapshot(env, make_session(env.paths.data_dir, env.clock), 3, "before_fault")
    raw_calls = [(call, cwd) for call, cwd in zip(env.commands.calls, env.commands.cwds) if call[0] != "ps"]
    primary, backup = env.paths.primary_state.parent, env.paths.backup_state.parent
    assert raw_calls == [(list(CAT_STATE), primary), (list(LS_LOCK), primary),
                         (list(CAT_STATE), backup), (list(LS_LOCK), backup)]
    assert not any("priv_validator_key" in argument for call in env.commands.calls for argument in call)


def test_take_snapshot_writes_the_raw_output_of_each_node(tmp_path):
    """raw/<iteration>-<point>-<node>.txt: '$ <command>', stdout, and for a failed command stderr and [exit <code>]."""
    env = snapshot_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    snapshot = take_snapshot(env, session, 3, "before_fault")
    assert [node["raw_file"] for node in snapshot["nodes"]] == ["raw/3-before_fault-validator1.txt",
                                                                "raw/3-before_fault-validator2.txt"]
    primary = (session.directory / "raw" / "3-before_fault-validator1.txt").read_text(encoding="utf-8")
    assert primary == (f"$ cat priv_validator_state.json\n{STATE_OUTPUT}\n"
                       f"$ ls -l priv_validator_state.json.lock\n{LOCK_LISTING}")
    backup = (session.directory / "raw" / "3-before_fault-validator2.txt").read_text(encoding="utf-8")
    assert backup.endswith(f"$ ls -l priv_validator_state.json.lock\n{NO_LOCK}[exit 1]\n")
    assert read_jsonl(session.directory / "snapshots.jsonl")[0]["nodes"][1]["raw_file"] == "raw/3-before_fault-validator2.txt"


def test_take_snapshot_writes_both_nodes_and_an_event(tmp_path):
    """snapshots.jsonl gets one row with both nodes; events.jsonl gets a snapshot event."""
    env = snapshot_env(tmp_path)
    session = make_session(env.paths.data_dir, env.clock)
    snapshot = take_snapshot(env, session, 3, "before_fault")
    rows = read_jsonl(session.directory / "snapshots.jsonl")
    assert rows == [snapshot]
    assert (snapshot["session"], snapshot["iteration"], snapshot["point"]) == ("S4", 3, "before_fault")
    assert [node["node"] for node in snapshot["nodes"]] == ["validator1", "validator2"]
    assert snapshot["nodes"][0]["locks"][0]["pid_alive"] is True
    event = read_events(session.directory / "events.jsonl")[-1]
    assert (event["kind"], event["point"], event["iteration"]) == ("snapshot", "before_fault", 3)
