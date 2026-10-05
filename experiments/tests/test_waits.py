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
