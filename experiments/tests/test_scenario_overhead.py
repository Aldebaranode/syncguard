"""Overhead sampler: CPU, RSS, and passive state writes."""

import os

import pytest

from fakes import READY_NOW, FakeClock, make_session, paths_under, ready_env
from harness.jsonio import read_json, read_jsonl
from harness.scenarios.common import PreconditionError
from harness.scenarios.overhead import (
    OverheadSettings,
    count_mtime_changes,
    parse_ps_sample,
    run_overhead,
)

PS_SAMPLE = (("ps", "-o", "pid=,pcpu=,rss=", "-p", "4242,4243"), [(0, " 4242   0.3  21344\n 4243   0.1  18000\n", "")])


class TouchingClock(FakeClock):
    """FakeClock that rewrites a file's mtime on every fifth 1-second sleep, like a state sync every 5 s."""

    def __init__(self, start_ms, path):
        """Remember the file to touch."""
        super().__init__(start_ms)
        self.path = path
        self.one_second_sleeps = 0

    def sleep(self, seconds):
        """Advance time and touch the file on every fifth 1-second sleep."""
        super().sleep(seconds)
        if seconds == 1.0:
            self.one_second_sleeps += 1
            if self.one_second_sleeps % 5 == 0:
                stamp = self.now * 1_000_000
                os.utime(self.path, ns=(stamp, stamp))


def test_parse_ps_sample():
    """pid, %cpu, rss per line."""
    samples = parse_ps_sample(PS_SAMPLE[1][0][1], 1000)
    assert [(s.pid, s.cpu_percent, s.rss_kb) for s in samples] == [(4242, 0.3, 21344), (4243, 0.1, 18000)]


def test_count_mtime_changes_skips_missing_readings():
    """Changes between consecutive known values."""
    assert count_mtime_changes([1, 1, 2, 2, None, 3, 3]) == 2


def test_run_overhead_samples_and_summarizes(tmp_path):
    """20 s: ps at 0, 5, 10, 15 s (8 samples); 3 state writes seen, 9 per minute."""
    clock = TouchingClock(READY_NOW, paths_under(tmp_path).backup_state)
    env = ready_env(tmp_path, extra_script=dict([PS_SAMPLE]), clock=clock)
    session = make_session(env.paths.data_dir, env.clock)
    summary = run_overhead(env, session, OverheadSettings(duration_s=20.0))
    assert len(read_jsonl(session.directory / "overhead.jsonl")) == 8
    assert summary["nodes"]["validator-1"]["cpu_percent"]["mean"] == pytest.approx(0.3)
    assert summary["nodes"]["validator-2"]["rss_kb"]["maximum"] == 18000
    assert summary["passive_state_writes"] == 3
    assert summary["passive_state_writes_per_minute"] == pytest.approx(9.0)
    assert read_json(session.directory / "overhead.json")["session"] == "S4"


def test_run_overhead_refuses_without_syncguard(tmp_path):
    """No sampling when the syncguard processes are not running."""
    env = ready_env(tmp_path, ps_output="", extra_script=dict([PS_SAMPLE]))
    session = make_session(env.paths.data_dir, env.clock)
    with pytest.raises(PreconditionError):
        run_overhead(env, session, OverheadSettings(duration_s=20.0))
    assert list(PS_SAMPLE[0]) not in env.commands.calls
