"""Session analysis: raw session files (events.jsonl, heights.jsonl, metadata.json) to analysis.json."""

from dataclasses import asdict
from pathlib import Path

from harness.constants import TARGET_VALIDATOR
from harness.jsonio import read_json, write_json
from harness.logparse import event_from_dict
from harness.metrics import failback_iteration, failover_iteration
from harness.session import read_events
from harness.signing import incident_block_rates, load_heights, missed_share, shift_to_host_clock


def fault_events(events: list, scenario: str) -> list:
    """Successful fault injections of one scenario, in file order."""
    return [event for event in events
            if event["kind"] == "fault_injected" and event.get("scenario") == scenario and event.get("exit_code") == 0]


def iteration_log_events(events: list, iteration: int) -> list:
    """The syncguard log events recorded for one iteration."""
    return [event_from_dict(event) for event in events
            if event["kind"] == "syncguard" and event.get("iteration") == iteration]


def incident_summary(records: list, failover, slashing_params: dict) -> dict:
    """Missed-block share and the three block-rate windows of one failover iteration.

    The share counts absent heights only: Story's x/slashing does not count nil votes as missed.
    """
    share = None
    if failover.absent_heights is not None:
        share = asdict(missed_share(failover.absent_heights, slashing_params["signed_blocks_window"],
                                    slashing_params["min_signed_per_window"]))
    rates = []
    if failover.t_first_signature_ms is not None:
        rates = [asdict(window) for window in
                 incident_block_rates(records, failover.t_fault_ms, failover.t_first_signature_ms)]
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


def soak_analysis(records: list, failover: list, failback: list, soak: dict,
                  validator: str = TARGET_VALIDATOR) -> dict:
    """Soak availability by equation 3.4 and, beside it, the block-based ratio of equation 3.8.

    D is the sum of the signing-gap durations of the validator inside the observation window: the
    TTR of each failover plus the G_fb of each failback, plus every other gap that no measured
    failover or failback interval explains.
    """
    window_s = (soak["end_ms"] - soak["start_ms"]) / 1000
    injected = _injected_intervals(failover, failback)
    other = [gap for gap in signing_gaps(records, validator) if not _overlaps(gap, injected)]
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


def analyze_session(directory: Path, validator: str = TARGET_VALIDATOR) -> dict:
    """Compute failover, failback, incident, hang, and soak results for one session and write analysis.json."""
    metadata = read_json(directory / "metadata.json")
    events = read_events(directory / "events.jsonl")
    records = shift_to_host_clock(load_heights(directory / "heights.jsonl"), metadata["clock_offset"]["offset_ms"])
    session_id = metadata["session"]
    config_id = metadata["config_id"]
    uncertainty_ms = metadata["clock_offset"]["offset_uncertainty_ms"]
    hang = sorted((read_json(path) for path in directory.glob("hang-*.json")), key=lambda row: row["iteration"])
    result = {"session": session_id, "config_id": config_id, "failover": [], "failback": [], "incidents": [],
              "hang": hang, "soak": None}
    for fault in fault_events(events, "failover"):
        iteration = fault["iteration"]
        log_events = iteration_log_events(events, iteration)
        failover = failover_iteration(session_id, iteration, config_id, fault["t_ms"],
                                      f"events.jsonl: {fault['command']}", log_events, records, validator,
                                      uncertainty_ms)
        failback = failback_iteration(session_id, iteration, config_id, fault["t_ms"], log_events, records, validator)
        result["failover"].append(asdict(failover))
        result["failback"].append(asdict(failback))
        result["incidents"].append(incident_summary(records, failover, metadata["slashing_params"]))
    soak_path = directory / "soak.json"
    if soak_path.exists():
        soak = read_json(soak_path)
        if soak.get("heights_collected"):
            result["soak"] = soak_analysis(records, result["failover"], result["failback"], soak, validator)
    write_json(directory / "analysis.json", result)
    return result
