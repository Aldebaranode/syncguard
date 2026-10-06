"""Dual-signer monitor: which key each node has loaded, once per second."""

import time
from types import SimpleNamespace

from fakes import FakeClock, FakeCommands, FakeHttp, load_fixture, node_status_answers, paths_under
from harness.adapters import Unreachable
from harness.constants import TARGET_VALIDATOR
from harness.environment import build_environment
from harness.jsonio import read_jsonl
from harness.monitor import (
    UNREACHABLE,
    DualSignerMonitor,
    MonitorSample,
    dual_signer_seconds,
    load_monitor_samples,
    sample_once,
    summarize_monitor,
)
from harness.rpc import CometRpc

MOCK = "E6FAA3F2B3E9158BA9BF93BE1F7F8D3095EAD04D"
BOTH = {"validator1-node": TARGET_VALIDATOR, "validator2-node": TARGET_VALIDATOR}
ONE = {"validator1-node": TARGET_VALIDATOR, "validator2-node": MOCK}
DOWN = {"validator1-node": UNREACHABLE, "validator2-node": TARGET_VALIDATOR}


def node_rpcs(http):
    """CometRpc clients for both validator nodes on one FakeHttp."""
    return {
        "validator1-node": CometRpc(http, "http://localhost:21657"),
        "validator2-node": CometRpc(http, "http://localhost:22657"),
    }


def test_sample_records_each_loaded_address():
    """validator1-node runs 48DC2183, validator2-node a mock key."""
    http = FakeHttp(node_status_answers("rpc/status_validator1.json", "rpc/status_validator2.json"))
    assert sample_once(node_rpcs(http), FakeClock(5000)) == MonitorSample(5000, ONE)


def test_unreachable_node_is_recorded_not_skipped():
    """A node that does not answer is written down as unreachable."""
    http = FakeHttp({
        "http://localhost:21657/status": Unreachable("connection refused"),
        "http://localhost:22657/status": load_fixture("rpc/status_validator1.json"),
    })
    assert sample_once(node_rpcs(http), FakeClock(5000)).addresses == DOWN


def test_dual_signer_seconds_counts_distinct_seconds():
    """Two samples in second 1 and one in second 2 make 2 seconds."""
    samples = [MonitorSample(1000, BOTH), MonitorSample(1500, BOTH), MonitorSample(2100, BOTH),
               MonitorSample(3000, ONE), MonitorSample(4000, DOWN)]
    assert dual_signer_seconds(samples, TARGET_VALIDATOR) == 2
    assert dual_signer_seconds(samples[3:], TARGET_VALIDATOR) == 0


def test_summary_counts_unreachable_per_node():
    """Sample count, span, dual-signer and indeterminate seconds, unreachable counts, seconds, and the largest gap."""
    samples = [MonitorSample(1000, BOTH), MonitorSample(3000, ONE), MonitorSample(4000, DOWN)]
    assert summarize_monitor(samples, TARGET_VALIDATOR) == {
        "samples": 3, "first_ms": 1000, "last_ms": 4000, "dual_signer_seconds": 1,
        "indeterminate_seconds": 1, "unreachable": {"validator1-node": 1},
        "unreachable_seconds": {"validator1-node": 1}, "max_gap_ms": 2000, "errors": 0,
    }
    assert summarize_monitor([], TARGET_VALIDATOR) == {
        "samples": 0, "first_ms": None, "last_ms": None, "dual_signer_seconds": 0, "indeterminate_seconds": 0,
        "unreachable": {}, "unreachable_seconds": {}, "max_gap_ms": 0, "errors": 0}


def test_one_node_unreachable_for_60_samples_is_indeterminate():
    """validator1-node down while validator2-node holds the validator key: 60 seconds nobody can vouch for."""
    samples = [MonitorSample(1000 * (10 + i), DOWN) for i in range(60)]
    summary = summarize_monitor(samples, TARGET_VALIDATOR)
    assert summary["indeterminate_seconds"] == 60
    assert summary["dual_signer_seconds"] == 0
    assert summary["unreachable_seconds"] == {"validator1-node": 60}


def test_error_samples_are_counted_and_do_not_count_as_observations():
    """A sample that records an exception has no addresses and is reported as an error."""
    samples = [MonitorSample(1000, BOTH), MonitorSample(2000, {}, error="RuntimeError('boom')")]
    summary = summarize_monitor(samples, TARGET_VALIDATOR)
    assert (summary["errors"], summary["dual_signer_seconds"]) == (1, 1)


def test_loop_exception_is_recorded_as_an_error_sample(tmp_path):
    """The thread survives an exception and writes an error sample that loads back."""

    class BrokenRpc:
        """RPC double whose call raises something other than Unreachable."""

        timeout = 0.8

        def loaded_address(self):
            """Always fail."""
            raise RuntimeError("boom")

    path = tmp_path / "monitor.jsonl"
    monitor = DualSignerMonitor({"validator1-node": BrokenRpc()}, FakeClock(7000), path, "S4", interval_s=0.01)
    monitor.start()
    deadline = time.monotonic() + 2.0
    while len(monitor.samples) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    samples = monitor.stop()
    assert len(samples) >= 2
    assert all(sample.error and "boom" in sample.error for sample in samples)
    assert monitor.errors
    assert load_monitor_samples(path) == samples


def test_monitor_thread_writes_every_sample(tmp_path):
    """start and stop live together; every sample lands in monitor.jsonl with the session id."""
    http = FakeHttp(node_status_answers("rpc/status_validator1.json", "rpc/status_validator2.json"))
    path = tmp_path / "monitor.jsonl"
    monitor = DualSignerMonitor(node_rpcs(http), FakeClock(7000), path, "S4", interval_s=0.01)
    monitor.start()
    deadline = time.monotonic() + 2.0
    while len(monitor.samples) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    samples = monitor.stop()
    assert len(samples) >= 3
    assert load_monitor_samples(path) == samples
    assert read_jsonl(path)[0]["session"] == "S4"


def test_monitor_for_session_uses_short_timeouts(tmp_path):
    """One hung node must not stretch the 1 s cadence much."""
    env = build_environment(FakeHttp(), FakeCommands(), FakeClock(0), paths_under(tmp_path), 25200)
    session = SimpleNamespace(id="S4", directory=tmp_path)
    monitor = DualSignerMonitor.for_session(env, session)
    assert {rpc.timeout for rpc in monitor.rpcs.values()} == {0.8}
    assert sorted(monitor.rpcs) == ["validator1-node", "validator2-node"]
    assert monitor.path == tmp_path / "monitor.jsonl"


def test_samples_since_returns_only_the_window(tmp_path):
    """One iteration of a series reads its own samples, not the earlier ones."""
    monitor = DualSignerMonitor({}, FakeClock(0), tmp_path / "m.jsonl", "S4")
    monitor.samples = [MonitorSample(1000, {}), MonitorSample(2000, {}), MonitorSample(3000, {})]
    assert [sample.t_ms for sample in monitor.samples_since(2000)] == [2000, 3000]
