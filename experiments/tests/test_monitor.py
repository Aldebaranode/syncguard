"""Dual-signer monitor: which key each node has loaded, once per second."""

import json
import time
from types import SimpleNamespace

import pytest

from fakes import FakeClock, FakeCommands, FakeHttp, load_fixture, node_status_answers, paths_under
from harness.adapters import Unreachable
from harness.constants import TARGET_VALIDATOR
from harness.environment import build_environment
from harness.jsonio import read_jsonl
from harness.monitor import (
    UNREACHABLE,
    DualSignerMonitor,
    ExecFallbackRpc,
    MonitorSample,
    coverage,
    dual_signer_seconds,
    informative,
    load_monitor_samples,
    sample_once,
    summarize_monitor,
    unobserved_intervals,
    votes_inside,
    window_report,
)
from harness.rpc import CometRpc
from harness.signing import HeightRecord

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


def every_second(start_ms: int, end_ms: int, addresses: dict = ONE) -> list:
    """One sample per second from start_ms to end_ms inclusive."""
    return [MonitorSample(t_ms, addresses) for t_ms in range(start_ms, end_ms + 1, 1000)]


def test_a_fully_sampled_window_is_observed():
    """Informative samples within 5 s of both ends and no gap over 5 s: observed, no problems."""
    assert coverage(every_second(0, 60_000), 2000, 58_000) == (True, [])


def test_a_30_second_hole_is_a_gap_problem_with_its_length():
    """The hole is reported as a code with its parameter, not as English text."""
    samples = [sample for sample in every_second(0, 120_000) if not 40_000 < sample.t_ms < 70_000]
    assert coverage(samples, 0, 120_000) == (False, [{"code": "gap", "ms": 30_000}])


def test_the_first_and_last_informative_samples_must_be_near_the_window_ends():
    """Samples that start 20 s late and stop 30 s early are two problems."""
    observed, problems = coverage(every_second(20_000, 50_000), 0, 80_000)
    assert observed is False
    assert problems == [{"code": "late_first_sample", "ms": 20_000}, {"code": "early_last_sample", "ms": 30_000}]


def test_a_window_without_samples_is_not_observed():
    """No samples at all is its own problem."""
    assert coverage([], 0, 1000) == (False, [{"code": "no_samples"}])


def test_samples_with_every_node_unreachable_are_not_informative():
    """A stretch where both nodes are unreachable leaves a hole in the coverage."""
    both_down = {"validator1-node": UNREACHABLE, "validator2-node": UNREACHABLE}
    samples = every_second(0, 20_000) + every_second(21_000, 40_000, both_down) + every_second(41_000, 60_000)
    assert coverage(samples, 0, 60_000) == (False, [{"code": "gap", "ms": 21_000}])


def test_a_sampling_error_near_the_window_is_a_problem():
    """An error sample within the window counts, with how many there were."""
    samples = every_second(0, 30_000) + [MonitorSample(15_500, {}, error="RuntimeError('boom')")]
    samples.sort(key=lambda sample: sample.t_ms)
    assert coverage(samples, 0, 30_000) == (False, [{"code": "sampling_errors", "count": 1}])


def test_indeterminate_seconds_are_a_separate_fact_not_a_coverage_problem():
    """One node unreachable while the other holds the key: observed, with the indeterminate seconds reported."""
    samples = every_second(0, 10_000) + every_second(11_000, 70_000, DOWN) + every_second(71_000, 80_000)
    window = {"scenario": "partition", "iteration": 3, "start_ms": 0, "end_ms": 80_000}
    report = window_report(samples, window, TARGET_VALIDATOR)
    assert (report["observed"], report["problems"]) == (True, [])
    assert report["indeterminate_seconds"] == 60
    assert report["dual_signer_seconds"] == 0
    assert (report["scenario"], report["iteration"]) == ("partition", 3)


def test_window_report_counts_dual_seconds_inside_the_window_only():
    """A dual-signer sample outside the window does not count for it."""
    samples = every_second(0, 30_000) + [MonitorSample(40_000, BOTH)]
    window = {"scenario": "failover", "iteration": 1, "start_ms": 0, "end_ms": 30_000}
    assert window_report(samples, window, TARGET_VALIDATOR)["dual_signer_seconds"] == 0


def legacy_coverage(samples: list, start_ms: int, end_ms: int) -> tuple:
    """coverage as written before it derived its problems from unobserved_intervals, kept as the reference result."""
    reach = 5000
    nearby = [sample for sample in samples if start_ms - reach <= sample.t_ms <= end_ms + reach]
    times = sorted(sample.t_ms for sample in nearby if informative(sample))
    errors = sum(1 for sample in nearby if sample.error is not None)
    problems = []
    if not times:
        problems.append({"code": "no_samples"})
    else:
        if times[0] > start_ms + reach:
            problems.append({"code": "late_first_sample", "ms": times[0] - start_ms})
        if times[-1] < end_ms - reach:
            problems.append({"code": "early_last_sample", "ms": end_ms - times[-1]})
        largest_gap = max((later - earlier for earlier, later in zip(times, times[1:])), default=0)
        if largest_gap > reach:
            problems.append({"code": "gap", "ms": largest_gap})
    if errors:
        problems.append({"code": "sampling_errors", "count": errors})
    return not problems, problems


BOTH_DOWN = {"validator1-node": UNREACHABLE, "validator2-node": UNREACHABLE}
ERROR_SAMPLE = MonitorSample(15_500, {}, error="RuntimeError('boom')")
COVERAGE_CASES = [
    (every_second(0, 60_000), 2000, 58_000),
    ([sample for sample in every_second(0, 120_000) if not 40_000 < sample.t_ms < 70_000], 0, 120_000),
    (every_second(20_000, 50_000), 0, 80_000),
    ([], 0, 1000),
    (every_second(0, 20_000) + every_second(21_000, 40_000, BOTH_DOWN) + every_second(41_000, 60_000), 0, 60_000),
    (sorted(every_second(0, 30_000) + [ERROR_SAMPLE], key=lambda sample: sample.t_ms), 0, 30_000),
    (every_second(0, 10_000) + every_second(11_000, 70_000, DOWN) + every_second(71_000, 80_000), 0, 80_000),
    ([sample for sample in every_second(0, 100_000)
      if not (10_000 < sample.t_ms < 20_000 or 50_000 < sample.t_ms < 80_000)], 0, 100_000),
    ([MonitorSample(30_000, ONE)], 0, 60_000),
    (every_second(-4000, 3000) + every_second(12_000, 30_000), 0, 30_000),
    (every_second(25_000, 40_000) + [ERROR_SAMPLE], 0, 60_000),
    ([MonitorSample(-6000, ONE)] + every_second(3000, 30_000), 0, 30_000),
]


@pytest.mark.parametrize("samples, start_ms, end_ms", COVERAGE_CASES)
def test_coverage_keeps_its_result_and_takes_its_lengths_from_the_unobserved_intervals(samples, start_ms, end_ms):
    """Same (observed, problems) as before the refactor; every length problem is the length of an unobserved
    interval, and only sampling errors make a window without unobserved intervals unobserved."""
    observed, problems = coverage(samples, start_ms, end_ms)
    assert (observed, problems) == legacy_coverage(samples, start_ms, end_ms)
    intervals = unobserved_intervals(samples, start_ms, end_ms)
    lengths = [end - start for start, end in intervals]
    for problem in problems:
        if "ms" in problem:
            assert problem["ms"] in lengths
    length_problems = [problem for problem in problems if problem["code"] != "sampling_errors"]
    assert bool(intervals) == bool(length_problems)


def test_unobserved_intervals_name_each_stretch_the_coverage_rule_cannot_vouch_for():
    """No informative sample: the whole window. A late first or early last sample: from the window edge to it.
    A hole: the two informative samples around it. A fully sampled window: none."""
    assert unobserved_intervals([], 0, 1000) == [(0, 1000)]
    assert unobserved_intervals(every_second(20_000, 50_000), 0, 80_000) == [(0, 20_000), (50_000, 80_000)]
    holes = [sample for sample in every_second(0, 120_000)
             if not (40_000 < sample.t_ms < 70_000 or 90_000 < sample.t_ms < 97_000)]
    assert unobserved_intervals(holes, 0, 120_000) == [(40_000, 70_000), (90_000, 97_000)]
    assert unobserved_intervals(every_second(0, 60_000), 2000, 58_000) == []


def test_unobserved_intervals_read_informative_samples_near_the_window_only():
    """Samples with both nodes unreachable or with an error leave a hole open, a sample beyond the 5 s reach is
    ignored, and two samples exactly 5 s apart leave no hole."""
    samples = (every_second(0, 20_000) + every_second(21_000, 40_000, BOTH_DOWN) + [ERROR_SAMPLE]
               + every_second(41_000, 60_000))
    assert unobserved_intervals(samples, 0, 60_000) == [(20_000, 41_000)]
    assert unobserved_intervals([MonitorSample(-6000, ONE)] + every_second(3000, 30_000), 0, 30_000) == []
    exact = [MonitorSample(0, ONE), MonitorSample(5000, ONE), MonitorSample(10_000, ONE)]
    assert unobserved_intervals(exact, 0, 10_000) == []


def height_record(height: int, header_ms: int, signers: dict | None = None,
                  nil_voters: dict | None = None) -> HeightRecord:
    """A host-clock height record with the given commit and nil votes."""
    return HeightRecord(height, header_ms, MOCK, 0, signers or {}, nil_voters or {}, 0)


VOTE_RECORDS = [
    height_record(10, 9_000, signers={TARGET_VALIDATOR: 10_000}),
    height_record(11, 12_000, nil_voters={TARGET_VALIDATOR: 15_000}),
    height_record(12, 19_500, signers={TARGET_VALIDATOR: 20_001, MOCK: 19_900}),
    height_record(13, 22_000, signers={MOCK: 22_500}),
]


def test_votes_inside_keeps_a_vote_inside_the_interval_and_drops_one_just_outside():
    """Both ends are inclusive: the commit vote at the start and the nil vote inside count; the vote 1 ms after the
    end does not, and another validator's vote is never the target's. Heights go by their header time."""
    assert votes_inside([(10_000, 20_000)], VOTE_RECORDS, TARGET_VALIDATOR) == [{
        "start_ms": 10_000, "end_ms": 20_000, "ms": 10_000, "heights": [11, 12],
        "votes": [{"height": 10, "t_ms": 10_000, "kind": "commit"}, {"height": 11, "t_ms": 15_000, "kind": "nil"}],
    }]


def test_votes_inside_gives_one_entry_per_interval_even_without_heights_or_votes():
    """No intervals, no entries; an interval with nothing in it is still listed, with empty lists."""
    assert votes_inside([], VOTE_RECORDS, TARGET_VALIDATOR) == []
    assert votes_inside([(0, 1000), (21_000, 25_000)], VOTE_RECORDS, TARGET_VALIDATOR) == [
        {"start_ms": 0, "end_ms": 1000, "ms": 1000, "heights": [], "votes": []},
        {"start_ms": 21_000, "end_ms": 25_000, "ms": 4000, "heights": [13], "votes": []},
    ]


EXEC_ARGV = ("docker", "exec", "validator1-node", "wget", "-qO-", "http://127.0.0.1:26657/status")


def test_primary_falls_back_to_the_loaded_key_read_inside_the_container():
    """With 21657 unreachable the key is read through docker exec; the sample is informative, not unreachable."""
    http = FakeHttp({
        "http://localhost:21657/status": Unreachable("connection refused"),
        "http://localhost:22657/status": load_fixture("rpc/status_validator2.json"),
    })
    commands = FakeCommands({EXEC_ARGV: [(0, json.dumps(load_fixture("rpc/status_validator1.json")), "")]})
    rpcs = node_rpcs(http)
    rpcs["validator1-node"] = ExecFallbackRpc(rpcs["validator1-node"], commands, "validator1-node")
    sample = sample_once(rpcs, FakeClock(5000))
    assert sample.addresses == ONE
    assert informative(sample)
    assert commands.calls == [list(EXEC_ARGV)]


def test_fallback_is_not_used_while_the_published_port_answers():
    """No docker exec when the RPC port is reachable."""
    http = FakeHttp({"http://localhost:21657/status": load_fixture("rpc/status_validator1.json")})
    commands = FakeCommands()
    rpc = ExecFallbackRpc(CometRpc(http, "http://localhost:21657"), commands, "validator1-node")
    assert rpc.loaded_address() == TARGET_VALIDATOR
    assert commands.calls == []


def test_fallback_that_fails_too_stays_unreachable():
    """A failed exec, or an answer that is not a status with an address, is Unreachable."""
    http = FakeHttp({"http://localhost:21657/status": Unreachable("down")})
    for answer in ((1, "", "No such container"), (0, "not json", ""), (0, '{"result": {"validator_info": {}}}', ""),
                   (0, '{"result": {"validator_info": {"address": ""}}}', ""),
                   (0, '{"result": {"validator_info": {"address": null}}}', "")):
        commands = FakeCommands({EXEC_ARGV: [answer]})
        rpc = ExecFallbackRpc(CometRpc(http, "http://localhost:21657"), commands, "validator1-node")
        with pytest.raises(Unreachable):
            rpc.loaded_address()


def test_for_session_gives_the_primary_the_fallback(tmp_path):
    """Only validator1-node gets the in-container source; the short timeout stays 0.8 s."""
    env = build_environment(FakeHttp(), FakeCommands(), FakeClock(0), paths_under(tmp_path), 25200)
    monitor = DualSignerMonitor.for_session(env, SimpleNamespace(id="S4", directory=tmp_path))
    assert isinstance(monitor.rpcs["validator1-node"], ExecFallbackRpc)
    assert not isinstance(monitor.rpcs["validator2-node"], ExecFallbackRpc)
