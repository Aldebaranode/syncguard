"""Session analysis: raw session files (events.jsonl, heights.jsonl, monitor.jsonl, metadata.json) to analysis.json."""

from dataclasses import asdict, fields
from pathlib import Path

from harness.constants import TARGET_VALIDATOR
from harness.jsonio import read_json, write_json
from harness.logparse import event_from_dict
from harness.metrics import FailoverIteration, failback_iteration, failover_iteration, seconds_between, took_over
from harness.monitor import load_monitor_samples, unobserved_intervals, votes_inside
from harness.session import read_events
from harness.signing import incident_block_rates, load_heights, missed_share, shift_to_host_clock
from harness.tenderduty import without_tenderduty_reads


def fault_events(events: list, scenario: str) -> list:
    """Successful fault injections of one scenario, in file order."""
    return [event for event in events
            if event["kind"] == "fault_injected" and event.get("scenario") == scenario and event.get("exit_code") == 0]


UNFOLLOWED_REASON = "perintah kill gagal; kontainer tidak berhenti"
STOPPED_STATE = "exited"


def injected_faults(events: list, scenario: str) -> list:
    """Every fault injection of one scenario with whether the runner followed it, in file order.

    A command that exited 0 was followed. A command that failed was followed only when the runner's
    fault_command_failed event found the container stopped; otherwise the iteration ended there.
    """
    failed_states = {event["iteration"]: event.get("container_state")
                     for event in events if event["kind"] == "fault_command_failed"}
    faults = []
    for event in events:
        if event["kind"] != "fault_injected" or event.get("scenario") != scenario:
            continue
        followed = event.get("exit_code") == 0 or failed_states.get(event["iteration"]) == STOPPED_STATE
        faults.append({"event": event, "followed": followed})
    return faults


def unfollowed_row(session_id: str, config_id: str, fault: dict, outcome: str | None) -> dict:
    """A failover row for an iteration the runner did not follow: identity, fault instant, outcome, no metrics."""
    row = {item.name: None for item in fields(FailoverIteration)}
    row.update(session=session_id, iteration=fault["iteration"], config_id=config_id, t_fault_ms=fault["t_ms"],
               invalid_reason=UNFOLLOWED_REASON, sources={"fault": f"events.jsonl: {fault['command']}"},
               outcome=outcome or "fault_command_failed", fault_exit_code=fault["exit_code"])
    return row


# The event that closes each scenario's run window; the monitor is still running when it is logged.
RUN_END_KINDS = {"failover": "iteration_end", "hang": "iteration_end", "partition": "iteration_end",
                 "baseline_drill": "restore_end"}
SOAK_END_KINDS = ("soak_end", "soak_aborted", "soak_interrupted")


def run_windows(events: list) -> list:
    """The interval each fault run must have under the dual-signer monitor, in file order.

    One window per fault_injected event: a failover iteration runs from its iteration_start to its
    iteration_end; a hang or partition run from the fault to its iteration_end; a drill from the kill
    to restore_end. A run without its end event (aborted) ends at its last event other than
    iteration_end, which a drill logs after its monitor stopped. A soak runs from soak_start to soak_end,
    or to its abort. A refused run has no fault and so no window; time between runs needs no samples.
    The reads of a Tenderduty end reading are left out (tenderduty.without_tenderduty_reads).
    """
    events = without_tenderduty_reads(events)
    windows = []
    for fault in (event for event in events if event["kind"] == "fault_injected"):
        scenario, iteration = fault.get("scenario"), fault.get("iteration")
        tagged = [event for event in events if event.get("iteration") == iteration and event["t_ms"] >= fault["t_ms"]]
        starts = [event["t_ms"] for event in events if event["kind"] == "iteration_start"
                  and event.get("iteration") == iteration and event["t_ms"] <= fault["t_ms"]]
        start_ms = starts[-1] if scenario == "failover" and starts else fault["t_ms"]
        ends = [event["t_ms"] for event in tagged if event["kind"] == RUN_END_KINDS.get(scenario)]
        if not ends:
            ends = [max((event["t_ms"] for event in tagged if event["kind"] != "iteration_end"), default=fault["t_ms"])]
        windows.append({"scenario": scenario, "iteration": iteration, "start_ms": start_ms, "end_ms": ends[0]})
    for start in (event for event in events if event["kind"] == "soak_start"):
        later = [event for event in events if event["t_ms"] >= start["t_ms"]]
        ends = [event["t_ms"] for event in later if event["kind"] in SOAK_END_KINDS]
        end_ms = ends[0] if ends else max(event["t_ms"] for event in later)
        windows.append({"scenario": "soak", "iteration": None, "start_ms": start["t_ms"], "end_ms": end_ms})
    return windows


def iteration_log_events(events: list, iteration: int) -> list:
    """The syncguard log events recorded for one iteration."""
    return [event_from_dict(event) for event in events
            if event["kind"] == "syncguard" and event.get("iteration") == iteration]


def iteration_outcomes(events: list) -> dict:
    """The outcome each iteration logged in its iteration_result event."""
    return {event["iteration"]: event["outcome"] for event in events if event["kind"] == "iteration_result"}


def aborted_series(events: list) -> list:
    """Iterations at which a failover series stopped early (series_aborted), with their outcomes."""
    return [{"iteration": event["iteration"], "outcome": event["outcome"]}
            for event in events if event["kind"] == "series_aborted" and event.get("scenario") == "failover"]


def hang_row(stored: dict, config_id: str, events: list, records: list, validator: str,
             uncertainty_ms: float | None) -> dict:
    """A hang-<n>.json row with the metrics the analysis owns: TTD, TTR by the failover rule, clock uncertainty.

    TTD is the runner's detection instant minus the pause instant. TTR, the heights missed during it, and
    why a TTR is invalid come from metrics.failover_iteration over the stored fault instant. A run whose
    pause never took effect has no fault instant and gets no metrics.
    """
    row = {**stored, "ttd_s": seconds_between(stored.get("t_fault_ms"), stored.get("t_detect_ms")),
           "ttr_s": None, "ttr_invalid_reason": None, "absent_heights": None, "clock_uncertainty_ms": uncertainty_ms}
    if not stored.get("fault_injected") or stored.get("t_fault_ms") is None:
        return row
    measured = failover_iteration(stored["session"], stored["iteration"], config_id, stored["t_fault_ms"],
                                  f"hang-{stored['iteration']}.json", iteration_log_events(events, stored["iteration"]),
                                  records, validator, uncertainty_ms)
    row.update(ttr_s=measured.ttr_s, ttr_invalid_reason=measured.invalid_reason, absent_heights=measured.absent_heights)
    return row


def incident_summary(records: list, failover, failback, slashing_params: dict) -> dict:
    """Missed-block share and the block-rate windows of one failover iteration.

    failback is the iteration's failback row, or None when the iteration had no takeover; its primary
    signature opens the fourth window. The share counts absent heights only: Story's x/slashing does
    not count nil votes as missed.
    """
    share = None
    if failover.absent_heights is not None:
        share = asdict(missed_share(failover.absent_heights, slashing_params["signed_blocks_window"],
                                    slashing_params["min_signed_per_window"]))
    primary_signature_ms = failback.t_primary_signature_ms if failback is not None else None
    rates = []
    if failover.t_first_signature_ms is not None:
        rates = [asdict(window) for window in
                 incident_block_rates(records, failover.t_fault_ms, failover.t_first_signature_ms, primary_signature_ms)]
    return {"session": failover.session, "iteration": failover.iteration, "config_id": failover.config_id,
            "missed_share": share, "block_rates": rates}


def signing_gaps(records: list, validator: str) -> list:
    """Every run of unsigned heights of the validator, measured from its last signature to its next one.

    A run touching the first or last stored height has no signature on that side, so it is bounded
    by that height's header time instead.
    """
    ordered = sorted(records, key=lambda record: record.height)
    gaps = []
    run = []
    last_signed = None
    for record in ordered:
        if validator in record.signers:
            if run:
                gaps.append(_gap(validator, run, last_signed, record))
                run = []
            last_signed = record
        else:
            run.append(record)
    if run:
        gaps.append(_gap(validator, run, last_signed, None))
    return gaps


def _gap(validator: str, run: list, signed_before, signed_after) -> dict:
    """One gap: the unsigned run and the signed records on each side (None at an edge of the range)."""
    start_ms = signed_before.signers[validator] if signed_before is not None else run[0].header_time_ms
    end_ms = signed_after.signers[validator] if signed_after is not None else run[-1].header_time_ms
    return {"first_unsigned_height": run[0].height, "last_unsigned_height": run[-1].height,
            "start_ms": start_ms, "end_ms": end_ms, "duration_s": (end_ms - start_ms) / 1000}


def _injected_intervals(failover: list, failback: list) -> list:
    """The (start_ms, end_ms) of every measured failover TTR and failback G_fb."""
    intervals = []
    for row in failover:
        if row["ttr_s"] is not None:
            intervals.append((row["t_fault_ms"], row["t_first_signature_ms"]))
    for row in failback:
        if row["gfb_s"] is not None:
            intervals.append((row["t_failback_notified_ms"], row["t_primary_signature_ms"]))
    return intervals


def _overlaps(gap: dict, intervals: list) -> bool:
    """Whether a signing gap intersects any measured failover or failback interval."""
    return any(gap["start_ms"] <= end and start <= gap["end_ms"] for start, end in intervals)


def _clipped(gap: dict, soak: dict) -> dict | None:
    """The part of a signing gap inside [start_ms, end_ms] of the soak, or None when none of it is inside."""
    start, end = max(gap["start_ms"], soak["start_ms"]), min(gap["end_ms"], soak["end_ms"])
    if end <= start:
        return None
    return {**gap, "start_ms": start, "end_ms": end, "duration_s": (end - start) / 1000}


def soak_analysis(records: list, failover: list, failback: list, soak: dict,
                  validator: str = TARGET_VALIDATOR) -> dict:
    """Soak availability by equation 3.4 and, beside it, the block-based ratio of equation 3.8.

    D is the sum of the signing-gap durations of the validator inside the observation window: the
    TTR of each failover plus the G_fb of each failback, plus every other gap that no measured
    failover or failback interval explains.
    """
    window_s = (soak["end_ms"] - soak["start_ms"]) / 1000
    injected = _injected_intervals(failover, failback)
    other = [gap for gap in (_clipped(gap, soak) for gap in signing_gaps(records, validator))
             if gap is not None and not _overlaps(gap, injected)]
    ttr = [row["ttr_s"] for row in failover if row["ttr_s"] is not None]
    gfb = [row["gfb_s"] for row in failback if row["gfb_s"] is not None]
    downtime_s = sum(ttr) + sum(gfb) + sum(gap["duration_s"] for gap in other)
    return {
        "window_s": window_s,
        "downtime_s": downtime_s,
        "availability": (window_s - downtime_s) / window_s,
        "availability_blocks": soak["availability"]["ratio"],
        "failover_ttr_s": sum(ttr) if ttr else None,
        "failback_gfb_s": sum(gfb) if gfb else None,
        "other_gaps": other,
    }


def monitor_gaps(directory: Path, events: list, records: list, validator: str) -> list | None:
    """Per run window, the intervals the monitor did not observe with the heights and the validator's votes inside them.

    The chain check beside the coverage rule: it reports what the chain recorded while no informative
    sample existed, and does not change any observed flag. records are the session's host-clock height
    records. None when the session has no monitor.jsonl or no heights.jsonl.
    """
    monitor_path = directory / "monitor.jsonl"
    if not monitor_path.exists() or not (directory / "heights.jsonl").exists():
        return None
    samples = load_monitor_samples(monitor_path)
    gaps = []
    for window in run_windows(events):
        intervals = unobserved_intervals(samples, window["start_ms"], window["end_ms"])
        gaps.append({"scenario": window["scenario"], "iteration": window["iteration"],
                     "intervals": votes_inside(intervals, records, validator)})
    return gaps


def monitor_gap_totals(gaps: list) -> dict:
    """Totals of one session's monitor_gaps block: intervals, the longest in ms, and the distinct votes and heights.

    Unobserved intervals of neighbouring run windows can overlap, so a vote or a height inside two of
    them is counted once.
    """
    intervals = [interval for window in gaps for interval in window["intervals"]]
    votes = {(vote["height"], vote["kind"], vote["t_ms"]) for interval in intervals for vote in interval["votes"]}
    heights = {height for interval in intervals for height in interval["heights"]}
    return {"intervals": len(intervals), "longest_ms": max((interval["ms"] for interval in intervals), default=0),
            "votes": len(votes), "heights": len(heights)}


def analyze_session(directory: Path, validator: str = TARGET_VALIDATOR) -> dict:
    """Compute failover, failback, incident, hang, partition, soak, and monitor-gap results and write analysis.json."""
    metadata = read_json(directory / "metadata.json")
    events = read_events(directory / "events.jsonl")
    records = shift_to_host_clock(load_heights(directory / "heights.jsonl"), metadata["clock_offset"]["offset_ms"])
    session_id = metadata["session"]
    config_id = metadata["config_id"]
    uncertainty_ms = metadata["clock_offset"]["offset_uncertainty_ms"]
    stored_hang = sorted((read_json(path) for path in directory.glob("hang-*.json")), key=lambda row: row["iteration"])
    hang = [hang_row(row, config_id, events, records, validator, uncertainty_ms) for row in stored_hang]
    partition = sorted((read_json(path) for path in directory.glob("partition-*.json")),
                       key=lambda row: row["iteration"])
    outcomes = iteration_outcomes(events)
    result = {"session": session_id, "config_id": config_id, "failover": [], "failback": [], "incidents": [],
              "hang": hang, "partition": partition, "soak": None, "series_aborted": aborted_series(events),
              "monitor_gaps": monitor_gaps(directory, events, records, validator)}
    for injected in injected_faults(events, "failover"):
        fault = injected["event"]
        iteration = fault["iteration"]
        if not injected["followed"]:
            result["failover"].append(unfollowed_row(session_id, config_id, fault, outcomes.get(iteration)))
            continue
        log_events = iteration_log_events(events, iteration)
        failover = failover_iteration(session_id, iteration, config_id, fault["t_ms"],
                                      f"events.jsonl: {fault['command']}", log_events, records, validator,
                                      uncertainty_ms)
        failback = failback_iteration(session_id, iteration, config_id, fault["t_ms"], log_events, records, validator)
        result["failover"].append({**asdict(failover), "outcome": outcomes.get(iteration),
                                   "fault_exit_code": fault["exit_code"]})
        recorded_failback = failback if took_over(failover.t_takeover_ms, failover.invalid_reason) else None
        if recorded_failback is not None:
            result["failback"].append(asdict(recorded_failback))
        result["incidents"].append(incident_summary(records, failover, recorded_failback, metadata["slashing_params"]))
    soak_path = directory / "soak.json"
    if soak_path.exists():
        soak = read_json(soak_path)
        if soak.get("heights_collected"):
            result["soak"] = soak_analysis(records, result["failover"], result["failback"], soak, validator)
    write_json(directory / "analysis.json", result)
    return result
