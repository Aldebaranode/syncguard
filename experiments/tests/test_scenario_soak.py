"""soak: 24 h with one failover, the monitor, availability, and pause detection."""

import pytest

from fakes import READY_NOW, TARGET_ADDRESS, FakeClock, FakeMonitor, FakeProbes, make_session, ready_env, write_key
from harness.jsonio import read_json, read_jsonl
from harness.logparse import LogEvent
from harness.scenarios.common import PreconditionError
from harness.scenarios.failover import FailoverSettings
from harness.scenarios.soak import SoakSettings, run_soak, wait_with_progress
from harness.session import read_events
from harness.signing import HeightRecord

KILL = ("docker", "kill", "validator1-node")
SHORT = SoakSettings(hours=0.01, kill_offset_hours=0.005, progress_interval_s=6.0)


class JumpClock(FakeClock):
    """FakeClock whose second sleep also jumps 200 s, as when the host sleeps."""

    def sleep(self, seconds):
        """Advance time; add the jump on the second sleep."""
        super().sleep(seconds)
        if len(self.slept) == 2:
            self.now += 200_000


def cycle_probes(now):
    """Answers for one complete failover cycle."""
    markers = {
        ("validator-2", "takeover_complete"): LogEvent("takeover_complete", "validator-2", now + 20000, "info", "m", "p:12"),
        ("validator-1", "failback_complete"): LogEvent("failback_complete", "validator-1", now + 100000, "info", "m", "a:22"),
        ("validator-2", "release_complete"): LogEvent("release_complete", "validator-2", now + 99000, "info", "m", "p:20"),
    }
    signatures = [HeightRecord(1200, now + 30000, "P", 0, {TARGET_ADDRESS: now + 30000}, {}, 0),
                  HeightRecord(1230, now + 115000, "P", 0, {TARGET_ADDRESS: now + 115000}, {}, 0)]
    return FakeProbes(markers, signatures)


def test_wait_with_progress_logs_pauses_and_stalls(tmp_path):
    """A 200 s jump is a host pause; an unchanged tip is a height stall."""
    env = ready_env(tmp_path, extra_http={"http://localhost:26657/status": {"result": {"sync_info": {"latest_block_height": "1188"}}}},
                    clock=JumpClock(READY_NOW))
    session = make_session(env.paths.data_dir, env.clock)
    wait_with_progress(env, session, READY_NOW + 300_000, 60.0)
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert kinds == ["host_pause", "height_stall"]
    assert [row["tip"] for row in read_jsonl(session.directory / "progress.jsonl")] == [1188, 1188]


def test_soak_runs_one_failover_inside_caffeinate_and_the_monitor(tmp_path):
    """caffeinate and the monitor start before and stop after the whole soak; availability from commits."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    summary = run_soak(env, session, SHORT, FailoverSettings(), cycle_probes(env.clock.now_ms()), monitor)
    assert [process.argv for process in env.commands.spawned] == [["caffeinate", "-dims"]]
    assert env.commands.spawned[0].stopped is True
    assert (monitor.started, monitor.stopped) == (1, 1)
    assert summary["kill_outcome"] == "complete"
    assert summary["availability"]["ratio"] == 1.0
    assert summary["header_gaps"] == []
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert kinds[1] == "soak_start" and kinds[-1] == "soak_end"
    assert kinds.count("fault_injected") == 1
    assert read_json(session.directory / "soak.json")["session"] == "S4"


def test_soak_refuses_before_caffeinate(tmp_path):
    """No caffeinate, no monitor, no kill when preconditions fail."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    write_key(env.paths.backup_key, TARGET_ADDRESS)
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(PreconditionError):
        run_soak(env, session, SHORT, FailoverSettings(), cycle_probes(env.clock.now_ms()), monitor)
    assert env.commands.spawned == []
    assert monitor.started == 0
