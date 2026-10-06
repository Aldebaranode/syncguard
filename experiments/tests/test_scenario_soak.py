"""soak: 24 h with one failover, the monitor, availability, and pause detection."""

import pytest

from fakes import (
    MOCK_ADDRESS,
    READY_NOW,
    TARGET_ADDRESS,
    FakeClock,
    FakeMonitor,
    FakeProbes,
    make_session,
    ready_env,
    rising_status,
    signed_commit_answer,
    write_key,
)
from harness.adapters import Unreachable
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
    assert kinds[1] == "soak_start" and kinds[-1] == "soak_availability"
    assert kinds.count("fault_injected") == 1
    assert read_json(session.directory / "soak.json")["session"] == "S4"


class EventAwareMonitor(FakeMonitor):
    """FakeMonitor that notes which events exist when it is stopped."""

    def __init__(self, session):
        """Watch one session's events."""
        super().__init__()
        self.session = session
        self.kinds_at_stop = None

    def stop(self):
        """Record the event kinds logged so far, then stop."""
        self.kinds_at_stop = [row["kind"] for row in read_events(self.session.directory / "events.jsonl")]
        return super().stop()


def test_soak_end_closes_the_observation_while_the_monitor_still_runs(tmp_path):
    """soak_end marks the end of the run window, so it is logged before the monitor stops and soak.json ends there."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    monitor = EventAwareMonitor(session)
    run_soak(env, session, SHORT, FailoverSettings(), cycle_probes(env.clock.now_ms()), monitor)
    assert "soak_end" in monitor.kinds_at_stop
    end = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "soak_end"][0]
    assert read_json(session.directory / "soak.json")["end_ms"] == end["t_ms"]


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


class InterruptingClock(FakeClock):
    """FakeClock that raises KeyboardInterrupt on its fifth sleep, like Ctrl-C during the soak wait."""

    def sleep(self, seconds):
        """Advance time; interrupt on the fifth sleep."""
        super().sleep(seconds)
        if len(self.slept) == 5:
            raise KeyboardInterrupt()


def test_soak_interruption_is_logged_and_everything_stops(tmp_path):
    """Ctrl-C mid-soak: soak_interrupted, caffeinate and the monitor stopped, collected heights kept, error re-raised."""
    env = ready_env(tmp_path, clock=InterruptingClock(READY_NOW))
    session = make_session(env.paths.data_dir, env.clock)
    monitor = FakeMonitor()
    with pytest.raises(KeyboardInterrupt):
        run_soak(env, session, SHORT, FailoverSettings(), FakeProbes({}, []), monitor)
    kinds = [row["kind"] for row in read_events(session.directory / "events.jsonl")]
    assert "soak_interrupted" in kinds and "soak_end" not in kinds
    assert env.commands.spawned[0].stopped is True
    assert (monitor.started, monitor.stopped) == (1, 1)
    assert len(read_jsonl(session.directory / "heights.jsonl")) > 0


def test_soak_summary_counts_signed_nil_absent_and_total_heights(tmp_path):
    """soak.json carries the height counts behind the availability ratio."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    summary = run_soak(env, session, SHORT, FailoverSettings(), cycle_probes(env.clock.now_ms()), FakeMonitor())
    stored = read_json(session.directory / "soak.json")
    assert stored["total_heights"] == summary["availability"]["total"] > 0
    assert (stored["signed_heights"], stored["nil_heights"], stored["absent_heights"]) == (stored["total_heights"], 0, 0)


def test_soak_transient_commit_error_keeps_collected_chunks(tmp_path):
    """A commit failure during the final collection raises, earlier chunks stay stored, and a rerun finishes the range."""
    state = {"final_phase_calls": 0, "enabled": True}

    def flaky_commit(url: str):
        """Fail every commit request of the final collection after its first twelve, while enabled."""
        events = read_events(session.directory / "events.jsonl")
        in_final_phase = sum(1 for row in events if row["kind"] == "heights_collected") >= 1
        if in_final_phase and state["enabled"]:
            state["final_phase_calls"] += 1
            if state["final_phase_calls"] > 12:
                return Unreachable("commit endpoint down")
        return signed_commit_answer([TARGET_ADDRESS, MOCK_ADDRESS])(url)

    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]},
                    extra_http={"http://localhost:26657/commit?height=*": flaky_commit,
                                "http://localhost:26657/status": rising_status(1188, step=15)})
    session = make_session(env.paths.data_dir, env.clock)
    settings = SoakSettings(hours=0.01, kill_offset_hours=0.005, progress_interval_s=6.0, chunk_size=5)
    with pytest.raises(Unreachable):
        run_soak(env, session, settings, FailoverSettings(), cycle_probes(env.clock.now_ms()), FakeMonitor())
    stored_after_failure = len(read_jsonl(session.directory / "heights.jsonl"))
    assert any(row["kind"] == "heights_retry" for row in read_events(session.directory / "events.jsonl"))
    assert state["final_phase_calls"] > 12  # chunks before the failing one were read and stored
    state["enabled"] = False
    summary = run_soak(env, session, settings, FailoverSettings(), cycle_probes(env.clock.now_ms()), FakeMonitor())
    assert len(read_jsonl(session.directory / "heights.jsonl")) >= stored_after_failure
    assert summary["availability"]["ratio"] == 1.0


def test_soak_json_is_written_before_the_final_heights(tmp_path):
    """A collection error at the end keeps soak.json (marked heights_collected false) and logs soak_aborted."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    working = env.http.answers["http://localhost:26657/commit?height=*"]

    def commits_fail_after_the_failover(url):
        """Normal commits until the failover iteration ended, then the rpc is down."""
        done = any(row["kind"] == "iteration_end" for row in read_events(session.directory / "events.jsonl"))
        return Unreachable("rpc down") if done else working(url)

    env.http.answers["http://localhost:26657/commit?height=*"] = commits_fail_after_the_failover
    settings = SoakSettings(hours=0.01, kill_offset_hours=0.005, progress_interval_s=6.0, chunk_size=5)
    with pytest.raises(Unreachable):
        run_soak(env, session, settings, FailoverSettings(), cycle_probes(env.clock.now_ms()), FakeMonitor())
    stored = read_json(session.directory / "soak.json")
    assert stored["heights_collected"] is False and stored["kill_outcome"] == "complete"
    assert any(row["kind"] == "soak_aborted" for row in read_events(session.directory / "events.jsonl"))


def test_end_of_soak_tip_read_failure_keeps_soak_json(tmp_path):
    """The tip read after the soak happens inside the abort scope and after soak.json is written."""
    env = ready_env(tmp_path, extra_script={KILL: [(0, "", "")]})
    session = make_session(env.paths.data_dir, env.clock)
    working = env.http.answers["http://localhost:26657/status"]

    def status_fails_once_soak_json_exists(url):
        """Normal status until the partial soak.json is on disk, then the rpc is down."""
        return Unreachable("rpc down") if (session.directory / "soak.json").exists() else working(url)

    env.http.answers["http://localhost:26657/status"] = status_fails_once_soak_json_exists
    with pytest.raises(Unreachable):
        run_soak(env, session, SHORT, FailoverSettings(), cycle_probes(env.clock.now_ms()), FakeMonitor())
    stored = read_json(session.directory / "soak.json")
    assert (stored["heights_collected"], stored["end_height"], stored["kill_outcome"]) == (False, None, "complete")
    assert any(row["kind"] == "soak_aborted" for row in read_events(session.directory / "events.jsonl"))
