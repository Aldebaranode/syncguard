"""Session end (R7): forward evidence scan, signing info, Prometheus series, monitor summary, log copies."""

import shutil
from dataclasses import asdict
from pathlib import Path

from harness.analysis import run_windows
from harness.clockoffset import measure_clock_offset
from harness.constants import CLOCK_CONTAINER, TARGET_PUBKEY_B64, TARGET_VALIDATOR
from harness.evidence import prometheus_maxima, scan_evidence
from harness.jsonio import write_json
from harness.monitor import informative, load_monitor_samples, summarize_monitor, window_report
from harness.session import read_events


def copy_logs(paths, directory: Path) -> list:
    """Copy both syncguard logs into the session so every file:line source stays checkable."""
    target = directory / "logs"
    target.mkdir(parents=True, exist_ok=True)
    copies = []
    for log in (paths.active_log, paths.passive_log):
        if log.exists():
            shutil.copy2(log, target / log.name)
            copies.append(f"logs/{log.name}")
    return copies


def monitor_report(samples: list, windows: list) -> dict:
    """Monitor summary of the session plus the coverage of every run window.

    The session is observed when every run window is observed; time between runs needs no samples.
    Each problem keeps the code from monitor.coverage and names the run it belongs to. Without an
    informative sample the dual-signer count was never measured, so it is None rather than 0.
    """
    summary = summarize_monitor(samples, TARGET_VALIDATOR)
    reports = [window_report(samples, window, TARGET_VALIDATOR) for window in windows]
    if not any(informative(sample) for sample in samples):
        summary["dual_signer_seconds"] = None
        summary["indeterminate_seconds"] = None
    summary["observed"] = bool(reports) and all(report["observed"] for report in reports)
    summary["observed_problems"] = [{**problem, "scenario": report["scenario"], "iteration": report["iteration"]}
                                    for report in reports for problem in report["problems"]]
    summary["windows"] = reports
    return summary


def clock_offset_at_finish(env, session) -> dict:
    """The container-minus-host offset measured again at finish and its drift from the session start, in ms.

    A failed measurement is recorded with its error instead of discarding the rest of the evidence.
    """
    try:
        end = measure_clock_offset(env.commands, CLOCK_CONTAINER)
    except RuntimeError as error:
        return {"clock_offset_end": None, "clock_drift_ms": None, "clock_offset_end_error": str(error)}
    drift = end.offset_ms - session.metadata["clock_offset"]["offset_ms"]
    return {"clock_offset_end": asdict(end), "clock_drift_ms": drift}


def collect_safety(env, session) -> dict:
    """Write safety.json: scan forward from the session's first fault height to the readable tip, and more.

    The logs are copied first, so a refused scan still leaves them behind. A session without a fault
    injection gets a logs-only finish: no safety.json and no Tabel 4.7 row, but its log lines still
    count for the session-wide checks such as unintended detections.
    """
    events = read_events(session.directory / "events.jsonl")
    logs = copy_logs(env.paths, session.directory)
    faults = [event for event in events if event["kind"] == "fault_injected"]
    if not faults:
        session.events.append("finish", logs_only=True)
        return {"session": session.id, "logs_only": True, "logs": logs}
    earliest = min(faults, key=lambda event: event["t_ms"])
    if "height" not in earliest:
        raise ValueError(f"session {session.id}: the earliest fault_injected event has no height to scan from")
    first = min(event["height"] for event in faults if "height" in event)
    last = env.rpc.tip_height()  # block evidence is final at the tip; the tip margin only applies to /commit signers
    scan = scan_evidence(env.rpc, first, last, env.rpc.evidence_params())
    now_ms = env.clock.now_ms()
    monitor_samples = load_monitor_samples(session.directory / "monitor.jsonl")
    safety = {
        "session": session.id,
        "t_ms": now_ms,
        "evidence_scan": asdict(scan),
        "signing_info": env.story.signing_info(TARGET_PUBKEY_B64),
        "prometheus": prometheus_maxima(env.prometheus, session.metadata["start_ms"] // 1000, now_ms // 1000),
        "monitor": monitor_report(monitor_samples, run_windows(events)),
        "logs": logs,
        **clock_offset_at_finish(env, session),
    }
    write_json(session.directory / "safety.json", safety)
    session.events.append("finish", evidence_first=first, evidence_last=last, evidence_found=len(scan.evidence))
    return safety
