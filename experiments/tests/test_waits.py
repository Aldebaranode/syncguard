"""Polling waits for markers, signatures, and missed heights."""

import shutil

from fakes import FIXTURES, FakeClock, FakeCommands, FakeHttp, commit_answers, load_fixture, make_session, paths_under
from harness.constants import BACKUP_NODE, TARGET_VALIDATOR
from harness.environment import build_environment
from harness.rpc import CometRpc
from harness.waits import HeightWatcher, LiveProbes, wait_until

FAULT_MS = 1791168828000


def tip_status(height: int) -> dict:
    """A /status answer with one fixed tip."""
    return {"jsonrpc": "2.0", "id": -1, "result": {"sync_info": {"latest_block_height": str(height)}}}


def smoke_http(tip: int = 147) -> FakeHttp:
    """rpc1 serving the smoke-test commits with a fixed tip."""
    return FakeHttp({
        "http://localhost:26657/status": tip_status(tip),
        "http://localhost:26657/commit?height=*": commit_answers(load_fixture("rpc/commits_smoke.json")),
    })


def probes_for(tmp_path, http):
    """LiveProbes on a fake environment and a session with zero clock offset."""
    clock = FakeClock(1791169200000)
    env = build_environment(http, FakeCommands(clock=clock), clock, paths_under(tmp_path), 25200)
    return LiveProbes(env, make_session(env.paths.data_dir, clock)), env


def test_wait_until_returns_the_first_value():
    """Polls until the check returns something."""
    answers = iter([None, None, "found"])
    clock = FakeClock(0)
    assert wait_until(lambda: next(answers), clock, 10) == "found"
    assert clock.slept == [1.0, 1.0]


def test_wait_until_gives_up_after_the_timeout():
    """None after timeout_s of polling."""
    clock = FakeClock(0)
    assert wait_until(lambda: None, clock, 3) is None
    assert clock.slept == [1.0, 1.0, 1.0]


def test_height_watcher_reads_each_height_once():
    """First signed height after the fault is 144; the next call continues at 145."""
    watcher = HeightWatcher(CometRpc(smoke_http(), "http://localhost:26657"), 0.0, 128)

    def signed(record) -> bool:
        """Signed by the validator at or after the fault."""
        return record.signers.get(TARGET_VALIDATOR, 0) >= FAULT_MS

    assert watcher.find(signed).height == 144
    assert watcher.find(signed).height == 145
    assert watcher.find(signed) is None


def test_probe_signature_and_miss(tmp_path):
    """LiveProbes finds the backup's first signature and the first missed height."""
    probes, _ = probes_for(tmp_path, smoke_http())
    assert probes.signature(FAULT_MS, 128, 10).height == 144
    assert probes.miss(126, 10).height == 128


def test_probe_marker_from_the_log(tmp_path):
    """The takeover line of the passive fixture; nothing after the last line."""
    probes, env = probes_for(tmp_path, smoke_http())
    env.paths.passive_log.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "logs" / "smoke-passive.txt", env.paths.passive_log)
    event = probes.marker(BACKUP_NODE, "takeover_complete", 1791168864636, 5)
    assert event.source == "syncguard-passive.log:12"
    assert probes.marker(BACKUP_NODE, "takeover_complete", 1791169000000, 2) is None


def test_probe_marker_without_a_log_waits_then_gives_up(tmp_path):
    """A log that does not exist yet is not an error."""
    probes, _ = probes_for(tmp_path, smoke_http())
    assert probes.marker(BACKUP_NODE, "takeover_complete", 0, 2) is None


TAKEOVER = ('time="2026-10-05 10:54:24.000" level=info msg="Successfully took over as active validator"\n')
OTHER = 'time="2026-10-05 10:54:25.000" level=info msg="routine line"\n'
RESTARTED = 'time="2026-10-05 10:54:26.000" level=info msg="Container validator1-node restarted"\n'


def test_log_tail_reads_only_the_bytes_added_since_the_last_read(tmp_path):
    """Each poll parses the new lines only; line numbers continue, and a half-written last line waits for its newline."""
    from harness.logparse import LogTail
    path = tmp_path / "syncguard.log"
    path.write_text(OTHER + TAKEOVER, encoding="utf-8")
    tail = LogTail(path, "validator-1", 25200)
    assert [event.source for event in tail.read_new()] == ["syncguard.log:2"]
    assert tail.read_new() == []
    with path.open("a", encoding="utf-8") as handle:
        handle.write(OTHER)
        handle.write(RESTARTED.rstrip("\n"))
    assert tail.read_new() == []
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert [(event.marker, event.source) for event in tail.read_new()] == [("container_restarted", "syncguard.log:4")]


def test_log_tail_starts_over_when_the_file_is_replaced_by_a_shorter_one(tmp_path):
    """A truncated or rotated log is read again from its first line."""
    from harness.logparse import LogTail
    path = tmp_path / "syncguard.log"
    path.write_text(OTHER + OTHER + TAKEOVER, encoding="utf-8")
    tail = LogTail(path, "validator-1", 25200)
    assert len(tail.read_new()) == 1
    path.write_text(TAKEOVER, encoding="utf-8")
    assert [event.source for event in tail.read_new()] == ["syncguard.log:1"]


def test_polling_a_marker_parses_each_log_line_once(tmp_path, monkeypatch):
    """A marker wait that polls while the log grows parses every line one time, not once per poll."""
    import harness.logparse as logparse
    probes, env = probes_for(tmp_path, smoke_http())
    path = env.paths.active_log
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(OTHER * 4, encoding="utf-8")
    parsed = []
    original = logparse.parse_line
    monkeypatch.setattr(logparse, "parse_line", lambda *args: parsed.append(args[1]) or original(*args))
    original_sleep = env.clock.sleep

    def append_then_sleep(seconds):
        """The log grows by one line between polls; the takeover line arrives on the third poll."""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(TAKEOVER if len(env.clock.slept) == 1 else OTHER)
        original_sleep(seconds)

    env.clock.sleep = append_then_sleep
    assert probes.marker("validator-1", "takeover_complete", 0, 10).source == "syncguard.log:6"
    assert sorted(parsed) == [1, 2, 3, 4, 5, 6]


def test_a_replaced_log_drops_the_events_of_the_old_one(tmp_path):
    """After a truncation the probes see only the new file's events."""
    probes, env = probes_for(tmp_path, smoke_http())
    path = env.paths.active_log
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(OTHER + OTHER + TAKEOVER, encoding="utf-8")
    assert len(probes.log_events("validator-1")) == 1
    path.write_text(RESTARTED, encoding="utf-8")
    assert [event.marker for event in probes.log_events("validator-1")] == ["container_restarted"]
