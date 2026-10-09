"""Session analysis on the fixture session built from the smoke-test recordings."""

import json

import pytest

from fakes import FIXTURES
from harness.analysis import (
    analyze_session,
    fault_events,
    monitor_gap_totals,
    run_windows,
    signing_gaps,
    soak_analysis,
)
from harness.constants import TARGET_VALIDATOR
from harness.dryrun import FIXTURE_FAULT_MS, build_fixture_session
from harness.jsonio import append_jsonl, read_json, write_json
from harness.session import read_events
from harness.signing import HeightRecord

OTHER = "7EFAECCD2CC260C216ECAFB28C245329B91F3CEF"


def test_fixture_session_layout(tmp_path):
    """Metadata from the recorded RPC answers, a fault event, tagged log events, 28 heights, and the log copies."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    metadata = read_json(directory / "metadata.json")
    assert metadata["session"] == "SFIX"
    assert "not a measurement" in metadata["purpose"]
    assert len(metadata["validators"]) == 4
    assert metadata["slashing_params"]["signed_blocks_window"] == 200
    assert metadata["clock_offset"]["offset_uncertainty_ms"] == 0.0
    events = read_events(directory / "events.jsonl")
    fault = [event for event in events if event["kind"] == "fault_injected"]
    assert [(event["t_ms"], event["height"]) for event in fault] == [(FIXTURE_FAULT_MS, 127)]
    assert sum(1 for event in events if event["kind"] == "syncguard") == 42
    assert len((directory / "heights.jsonl").read_text().splitlines()) == 28
    assert (directory / "logs" / "smoke-active.txt").exists()
    assert (directory / "logs" / "smoke-passive.txt").exists()
    assert build_fixture_session(FIXTURES, tmp_path) == directory


def test_analysis_of_the_fixture_session(tmp_path):
    """The numbers of the metrics tests, now through events.jsonl and heights.jsonl."""
    result = analyze_session(build_fixture_session(FIXTURES, tmp_path))
    failover = result["failover"][0]
    assert failover["ttr_s"] == pytest.approx(68.910)
    assert failover["ttd_s"] == pytest.approx(51.620)
    assert failover["clock_uncertainty_ms"] == 0.0
    assert failover["sources"]["fault"] == "events.jsonl: docker kill validator1-node (declared fixture input)"
    assert failover["sources"]["detect"] == "smoke-active.txt:7"
    assert result["failback"][0]["to_signature_s"] == pytest.approx(76.803)
    assert result["failback"][0]["gfb_s"] == pytest.approx(16.798)
    incident = result["incidents"][0]
    assert (incident["missed_share"]["missed"], incident["missed_share"]["allowance"]) == (15, 190)
    assert [window["covered"] for window in incident["block_rates"]] == [False, False, False]
    assert result["soak"] is None
    assert read_json(tmp_path / "SFIX" / "analysis.json") == result


def test_fault_events_keep_successful_injections_of_one_scenario():
    """Failed commands and other scenarios are left out."""
    events = [
        {"kind": "fault_injected", "scenario": "failover", "exit_code": 0, "iteration": 1},
        {"kind": "fault_injected", "scenario": "failover", "exit_code": 1, "iteration": 2},
        {"kind": "fault_injected", "scenario": "hang", "exit_code": 0, "iteration": 3},
        {"kind": "observed", "iteration": 1},
    ]
    assert [event["iteration"] for event in fault_events(events, "failover")] == [1]


def test_hang_results_are_ordered_by_iteration(tmp_path):
    """hang-10.json sorts after hang-2.json."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    write_json(directory / "hang-10.json", {"session": "SFIX", "iteration": 10})
    write_json(directory / "hang-2.json", {"session": "SFIX", "iteration": 2})
    assert [row["iteration"] for row in analyze_session(directory)["hang"]] == [2, 10]


def test_partition_results_are_ordered_by_iteration_and_default_to_none(tmp_path):
    """partition-10.json sorts after partition-2.json; a session without any has an empty list."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    assert analyze_session(directory)["partition"] == []
    write_json(directory / "partition-10.json", {"session": "SFIX", "iteration": 10})
    write_json(directory / "partition-2.json", {"session": "SFIX", "iteration": 2})
    assert [row["iteration"] for row in analyze_session(directory)["partition"]] == [2, 10]


def soak_records(unsigned: set) -> list:
    """100 heights, 3 s apart from t = 0; the validator signs 1 s after each header except at the unsigned heights."""
    records = []
    for height in range(1, 101):
        header_ms = height * 3000
        signers = {OTHER: header_ms + 500}
        if height not in unsigned:
            signers[TARGET_VALIDATOR] = header_ms + 1000
        records.append(HeightRecord(height, header_ms, OTHER, 0, signers, {}, 0))
    return records


def test_signing_gaps_measure_from_the_last_to_the_next_signature():
    """Heights 40 to 45 unsigned: the gap runs from the signature at height 39 to the one at height 46."""
    gaps = signing_gaps(soak_records(set(range(40, 46))), TARGET_VALIDATOR)
    assert gaps == [{"first_unsigned_height": 40, "last_unsigned_height": 45, "start_ms": 39 * 3000 + 1000,
                     "end_ms": 46 * 3000 + 1000, "duration_s": 21.0}]


def test_signing_gaps_at_the_edges_run_to_the_first_or_last_header_time():
    """A run touching the first height starts at its header time; one touching the last height ends at its header time."""
    gaps = signing_gaps(soak_records({1, 2, 99, 100}), TARGET_VALIDATOR)
    assert [(gap["first_unsigned_height"], gap["start_ms"], gap["end_ms"]) for gap in gaps] == [
        (1, 3000, 3 * 3000 + 1000), (99, 98 * 3000 + 1000, 100 * 3000)]


def test_soak_downtime_is_ttr_plus_gfb_plus_every_other_gap():
    """D = TTR + G_fb + the unrelated gap; availability is (T - D) / T and the block ratio is reported beside it."""
    unsigned = set(range(40, 46)) | set(range(60, 63)) | set(range(80, 83))
    records = soak_records(unsigned)
    failover = [{"t_fault_ms": 39 * 3000 + 2000, "t_first_signature_ms": 46 * 3000 + 1000, "ttr_s": 19.0}]
    failback = [{"t_failback_notified_ms": 79 * 3000 + 2000, "t_primary_signature_ms": 83 * 3000 + 1000, "gfb_s": 10.0}]
    soak = {"start_ms": 3000, "end_ms": 303000, "availability": {"signed": 88, "total": 100, "ratio": 0.88}}
    result = soak_analysis(records, failover, failback, soak)
    assert result["window_s"] == 300.0
    other = result["other_gaps"]
    assert [(gap["first_unsigned_height"], gap["duration_s"]) for gap in other] == [(60, 12.0)]
    assert result["failover_ttr_s"] == 19.0
    assert result["failback_gfb_s"] == 10.0
    assert result["downtime_s"] == pytest.approx(19.0 + 10.0 + 12.0)
    assert result["availability"] == pytest.approx((300 - 41) / 300)
    assert result["availability_blocks"] == 0.88


def test_soak_without_a_valid_failover_counts_its_gap_as_other_downtime():
    """A failover row with no TTR does not hide the gap: the unsigned run is counted as another gap."""
    records = soak_records(set(range(40, 46)))
    failover = [{"t_fault_ms": 39 * 3000 + 2000, "t_first_signature_ms": None, "ttr_s": None}]
    soak = {"start_ms": 3000, "end_ms": 303000, "availability": {"signed": 94, "total": 100, "ratio": 0.94}}
    result = soak_analysis(records, failover, [], soak)
    assert result["failover_ttr_s"] is None
    assert result["downtime_s"] == pytest.approx(21.0)


def test_analyze_session_adds_the_soak_block_when_soak_json_has_heights(tmp_path):
    """soak.json with heights_collected true makes analysis.json carry a soak block; without it the block is None."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    write_json(directory / "soak.json", {"session": "SFIX", "heights_collected": True, "start_ms": 1791168800000,
                                         "end_ms": 1791169000000,
                                         "availability": {"signed": 20, "total": 28, "ratio": 20 / 28}})
    soak = analyze_session(directory)["soak"]
    assert soak["window_s"] == 200.0
    assert soak["availability_blocks"] == pytest.approx(20 / 28)
    write_json(directory / "soak.json", {"session": "SFIX", "heights_collected": False, "start_ms": 1, "end_ms": 2})
    assert analyze_session(directory)["soak"] is None


def test_incomplete_series_is_visible_in_the_analysis(tmp_path):
    """iteration_result outcomes ride on the failover rows and series_aborted events are listed."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    append_jsonl(directory / "events.jsonl", {"session": "SFIX", "t_ms": FIXTURE_FAULT_MS + 200_000, "kind": "iteration_result",
                                              "scenario": "failover", "iteration": 1, "outcome": "no_failback"})
    append_jsonl(directory / "events.jsonl", {"session": "SFIX", "t_ms": FIXTURE_FAULT_MS + 201_000, "kind": "series_aborted",
                                              "scenario": "failover", "iteration": 1, "outcome": "no_failback"})
    result = analyze_session(directory)
    assert result["failover"][0]["outcome"] == "no_failback"
    assert result["series_aborted"] == [{"iteration": 1, "outcome": "no_failback"}]


def test_a_partition_series_stop_is_not_a_failover_series_stop(tmp_path):
    """series_aborted lists failover series only; a partition run that stopped has its own outcome in partition-N.json."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    append_jsonl(directory / "events.jsonl", {"session": "SFIX", "t_ms": FIXTURE_FAULT_MS + 201_000, "kind": "series_aborted",
                                              "scenario": "partition", "iteration": 2, "outcome": "not_detected"})
    assert analyze_session(directory)["series_aborted"] == []


def test_a_complete_series_has_no_aborted_entry(tmp_path):
    """Without a series_aborted event the list is empty and an iteration without a result has outcome None."""
    result = analyze_session(build_fixture_session(FIXTURES, tmp_path))
    assert result["series_aborted"] == []
    assert result["failover"][0]["outcome"] == "complete"


def add_failed_kill(directory, iteration: int, state: str) -> None:
    """Append a failed kill command (exit 1) and the runner's fault_command_failed event with the container state."""
    base = {"session": "SFIX", "scenario": "failover", "iteration": iteration}
    append_jsonl(directory / "events.jsonl", {**base, "t_ms": FIXTURE_FAULT_MS + 300_000, "kind": "fault_injected",
                                              "action": "kill", "command": "docker kill validator1-node", "exit_code": 1,
                                              "ended_ms": FIXTURE_FAULT_MS + 300_100, "height": 170})
    append_jsonl(directory / "events.jsonl", {**base, "t_ms": FIXTURE_FAULT_MS + 300_200, "kind": "fault_command_failed",
                                              "exit_code": 1, "container_state": state})


def test_a_failed_kill_that_stopped_the_container_is_followed_and_marked(tmp_path):
    """The runner follows such an iteration, so the analysis keeps it and records the command's exit code."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    add_failed_kill(directory, 2, "exited")
    rows = analyze_session(directory)["failover"]
    assert [(row["iteration"], row["fault_exit_code"]) for row in rows] == [(1, 0), (2, 1)]
    assert rows[1]["outcome"] is None


def test_a_failed_kill_that_was_not_followed_appears_with_outcome_and_no_metrics(tmp_path):
    """Container still running after the failed kill: a row with the runner's outcome and empty metrics, no failback."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    add_failed_kill(directory, 2, "running")
    append_jsonl(directory / "events.jsonl", {"session": "SFIX", "t_ms": FIXTURE_FAULT_MS + 301_000, "kind": "iteration_result",
                                              "scenario": "failover", "iteration": 2, "outcome": "fault_command_failed"})
    result = analyze_session(directory)
    row = result["failover"][1]
    assert (row["iteration"], row["outcome"], row["fault_exit_code"]) == (2, "fault_command_failed", 1)
    assert row["ttr_s"] is None and row["ttd_s"] is None
    assert "kontainer tidak berhenti" in row["invalid_reason"]
    assert [item["iteration"] for item in result["failback"]] == [1]
    assert [item["iteration"] for item in result["incidents"]] == [1]


def test_failback_rows_exist_only_for_iterations_with_a_takeover(tmp_path):
    """Without a takeover marker there is no failback to measure."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    rows = [row for row in read_events(directory / "events.jsonl") if row.get("marker") != "takeover_complete"]
    (directory / "events.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    result = analyze_session(directory)
    assert result["failover"][0]["invalid_reason"] == "tanpa pengambilalihan"
    assert result["failback"] == []


def test_soak_gaps_are_clipped_to_the_observation_window():
    """A gap that starts before the window counts only its part inside; a gap wholly outside is dropped."""
    records = soak_records(set(range(10, 13)) | {50})
    soak = {"start_ms": 45_000, "end_ms": 303_000, "availability": {"signed": 97, "total": 100, "ratio": 0.97}}
    result = soak_analysis(records, [], [], soak)
    assert [(gap["first_unsigned_height"], gap["start_ms"], gap["duration_s"]) for gap in result["other_gaps"]] == [
        (50, 148_000, 6.0)]
    inside = soak_analysis(records, [], [], {**soak, "start_ms": 33_000})
    assert inside["other_gaps"][0]["first_unsigned_height"] == 10
    assert inside["other_gaps"][0]["start_ms"] == 33_000
    assert inside["other_gaps"][0]["duration_s"] == pytest.approx((13 * 3000 + 1000 - 33_000) / 1000)


def test_run_windows_follow_the_rule_of_each_scenario():
    """failover: iteration_start to iteration_end; hang and partition: fault to iteration_end (or the last event of
    an aborted run); drill: kill to restore_end; soak: soak_start to soak_end. A refused run has no window."""
    events = [
        {"kind": "precondition_failed", "scenario": "failover", "iteration": 1, "t_ms": 500},
        {"kind": "iteration_start", "scenario": "failover", "iteration": 1, "t_ms": 1000},
        {"kind": "fault_injected", "scenario": "failover", "iteration": 1, "t_ms": 5000, "exit_code": 0},
        {"kind": "iteration_end", "scenario": "failover", "iteration": 1, "t_ms": 300_000},
        {"kind": "iteration_start", "scenario": "hang", "iteration": 2, "t_ms": 390_000},
        {"kind": "fault_injected", "scenario": "hang", "iteration": 2, "t_ms": 400_000, "exit_code": 0},
        {"kind": "iteration_end", "scenario": "hang", "iteration": 2, "t_ms": 700_000},
        {"kind": "fault_injected", "scenario": "partition", "iteration": 3, "t_ms": 800_000, "exit_code": 0},
        {"kind": "iteration_aborted", "scenario": "partition", "iteration": 3, "t_ms": 900_000},
        {"kind": "abort_reconnect", "scenario": "partition", "iteration": 3, "t_ms": 905_000},
        {"kind": "fault_injected", "scenario": "baseline_drill", "iteration": 4, "t_ms": 1_000_000, "exit_code": 0},
        {"kind": "restore_end", "iteration": 4, "t_ms": 1_500_000},
        {"kind": "iteration_end", "scenario": "baseline_drill", "iteration": 4, "t_ms": 1_600_000},
        {"kind": "soak_start", "t_ms": 2_000_000},
        {"kind": "soak_end", "t_ms": 3_000_000},
        {"kind": "precondition_failed", "scenario": "hang", "iteration": 5, "t_ms": 3_100_000},
    ]
    assert run_windows(events) == [
        {"scenario": "failover", "iteration": 1, "start_ms": 1000, "end_ms": 300_000},
        {"scenario": "hang", "iteration": 2, "start_ms": 400_000, "end_ms": 700_000},
        {"scenario": "partition", "iteration": 3, "start_ms": 800_000, "end_ms": 905_000},
        {"scenario": "baseline_drill", "iteration": 4, "start_ms": 1_000_000, "end_ms": 1_500_000},
        {"scenario": "soak", "iteration": None, "start_ms": 2_000_000, "end_ms": 3_000_000},
    ]


def test_an_unfinished_drill_window_ends_before_its_iteration_end():
    """A drill whose kill failed has no restore_end; its window ends at its last event before iteration_end,
    because the drill stops the monitor before it collects heights and logs iteration_end."""
    events = [
        {"kind": "fault_injected", "scenario": "baseline_drill", "iteration": 1, "t_ms": 1000, "exit_code": 1},
        {"kind": "drill_kill_failed", "iteration": 1, "t_ms": 2000},
        {"kind": "iteration_end", "scenario": "baseline_drill", "iteration": 1, "t_ms": 90_000},
        {"kind": "soak_start", "t_ms": 100_000},
        {"kind": "soak_interrupted", "scenario": "soak", "iteration": None, "t_ms": 150_000},
    ]
    assert run_windows(events) == [
        {"scenario": "baseline_drill", "iteration": 1, "start_ms": 1000, "end_ms": 2000},
        {"scenario": "soak", "iteration": None, "start_ms": 100_000, "end_ms": 150_000},
    ]


def test_the_fixture_iteration_ends_after_its_last_log_event(tmp_path):
    """The dry-run session stamps iteration_result and iteration_end after the recorded cycle, so its run window
    spans the failover and the failback it holds."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    events = read_events(directory / "events.jsonl")
    last_log = max(event["t_ms"] for event in events if event["kind"] == "syncguard")
    [window] = run_windows(events)
    assert window["start_ms"] < FIXTURE_FAULT_MS and window["end_ms"] > last_log


def test_hang_metrics_are_computed_by_the_analysis_with_the_clock_uncertainty(tmp_path):
    """TTD from the runner's detection instant, TTR by the failover rule from the stored heights, and the session's
    clock uncertainty, added to the hang row; an injection that never took effect gets no metrics."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    events = read_events(directory / "events.jsonl")
    rows = [{**event, "scenario": "hang"} if event.get("scenario") == "failover" else event for event in events]
    (directory / "events.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    write_json(directory / "hang-1.json", {"session": "SFIX", "iteration": 1, "fault_injected": True,
                                            "t_fault_ms": FIXTURE_FAULT_MS, "t_detect_ms": FIXTURE_FAULT_MS + 51_620})
    write_json(directory / "hang-2.json", {"session": "SFIX", "iteration": 2, "fault_injected": False,
                                            "t_fault_ms": None, "t_detect_ms": None})
    result = analyze_session(directory)
    assert result["failover"] == []
    first, second = result["hang"]
    assert first["ttd_s"] == pytest.approx(51.620)
    assert first["ttr_s"] == pytest.approx(68.910)
    assert (first["ttr_invalid_reason"], first["absent_heights"], first["clock_uncertainty_ms"]) == (None, 15, 0.0)
    assert (second["ttd_s"], second["ttr_s"]) == (None, None)


MOCK = "E6FAA3F2B3E9158BA9BF93BE1F7F8D3095EAD04D"
HOLE_START_MS = FIXTURE_FAULT_MS + 60_000
HOLE_END_MS = FIXTURE_FAULT_MS + 75_000


def write_monitor(directory, hole: tuple | None = None) -> None:
    """monitor.jsonl with one informative sample per second across the fixture window, none strictly inside hole."""
    path = directory / "monitor.jsonl"
    path.write_text("")
    for t_ms in range(FIXTURE_FAULT_MS - 60_000, FIXTURE_FAULT_MS + 160_001, 1000):
        if hole is not None and hole[0] < t_ms < hole[1]:
            continue
        append_jsonl(path, {"session": "SFIX", "t_ms": t_ms,
                            "addresses": {"validator1-node": TARGET_VALIDATOR, "validator2-node": MOCK}})


def test_monitor_gaps_list_the_heights_and_votes_inside_each_unobserved_interval(tmp_path):
    """A 15 s monitor hole after the fault: heights 143 to 145 lie in it, with the nil vote of 48DC2183 at 143 and
    its commit votes at 144 and 145; the block is written to analysis.json."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    write_monitor(directory, hole=(HOLE_START_MS, HOLE_END_MS))
    result = analyze_session(directory)
    assert result["monitor_gaps"] == [{"scenario": "failover", "iteration": 1, "intervals": [{
        "start_ms": HOLE_START_MS, "end_ms": HOLE_END_MS, "ms": 15_000, "heights": [143, 144, 145],
        "votes": [{"height": 143, "t_ms": FIXTURE_FAULT_MS + 66_098, "kind": "nil"},
                  {"height": 144, "t_ms": FIXTURE_FAULT_MS + 68_910, "kind": "commit"},
                  {"height": 145, "t_ms": FIXTURE_FAULT_MS + 71_756, "kind": "commit"}]}]}]
    assert read_json(directory / "analysis.json")["monitor_gaps"] == result["monitor_gaps"]


def test_monitor_gaps_compare_host_clock_votes_with_the_samples(tmp_path):
    """With a container clock 1 s ahead, every vote is shifted onto the host clock before it is matched."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    metadata = read_json(directory / "metadata.json")
    metadata["clock_offset"]["offset_ms"] = 1000.0
    write_json(directory / "metadata.json", metadata)
    write_monitor(directory, hole=(HOLE_START_MS, HOLE_END_MS))
    [interval] = analyze_session(directory)["monitor_gaps"][0]["intervals"]
    assert [vote["t_ms"] for vote in interval["votes"]] == [
        FIXTURE_FAULT_MS + 65_098, FIXTURE_FAULT_MS + 67_910, FIXTURE_FAULT_MS + 70_756]


def test_a_fully_observed_window_has_an_empty_interval_list(tmp_path):
    """Samples every second across the window: one entry for the run, without intervals."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    write_monitor(directory)
    assert analyze_session(directory)["monitor_gaps"] == [{"scenario": "failover", "iteration": 1, "intervals": []}]


def test_monitor_gaps_need_the_monitor_and_the_heights(tmp_path):
    """No monitor.jsonl or no heights.jsonl: the block is None; a session without run windows gives an empty list."""
    directory = build_fixture_session(FIXTURES, tmp_path)
    assert analyze_session(directory)["monitor_gaps"] is None
    write_monitor(directory)
    (directory / "heights.jsonl").unlink()
    assert analyze_session(directory)["monitor_gaps"] is None
    directory = build_fixture_session(FIXTURES, tmp_path)
    write_monitor(directory)
    rows = [row for row in read_events(directory / "events.jsonl") if row["kind"] != "fault_injected"]
    (directory / "events.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    assert analyze_session(directory)["monitor_gaps"] == []


def gap_interval(start_ms: int, end_ms: int, heights: list, votes: list) -> dict:
    """One entry of votes_inside; votes are (height, t_ms, kind) triples."""
    return {"start_ms": start_ms, "end_ms": end_ms, "ms": end_ms - start_ms, "heights": heights,
            "votes": [{"height": height, "t_ms": t_ms, "kind": kind} for height, t_ms, kind in votes]}


def test_monitor_gap_totals_count_intervals_the_longest_and_distinct_votes_and_heights():
    """Each interval counts; a vote or height inside two overlapping intervals of neighbouring runs counts once."""
    block = [
        {"scenario": "failover", "iteration": 1, "intervals": [
            gap_interval(0, 15_000, [10, 11], [(10, 4000, "commit"), (11, 7000, "nil")]),
            gap_interval(20_000, 47_000, [12], [(12, 30_000, "commit")])]},
        {"scenario": "failover", "iteration": 2, "intervals": [
            gap_interval(40_000, 50_000, [12, 13], [(13, 45_000, "commit")])]},
        {"scenario": "failover", "iteration": 3, "intervals": []},
    ]
    assert monitor_gap_totals(block) == {"intervals": 3, "longest_ms": 27_000, "votes": 4, "heights": 4}
    overlapping = [
        {"scenario": "failover", "iteration": 1, "intervals": [gap_interval(0, 10_000, [5], [(5, 6000, "commit")])]},
        {"scenario": "failover", "iteration": 2, "intervals": [
            gap_interval(4000, 12_000, [5, 6], [(5, 6000, "commit"), (6, 11_000, "commit")])]},
    ]
    assert monitor_gap_totals(overlapping) == {"intervals": 2, "longest_ms": 10_000, "votes": 2, "heights": 2}
    assert monitor_gap_totals([]) == {"intervals": 0, "longest_ms": 0, "votes": 0, "heights": 0}
