"""Session end (R7): forward evidence scan, signing info, Prometheus series, monitor summary, log copies."""

import shutil
from dataclasses import asdict
from pathlib import Path

from harness.constants import TARGET_PUBKEY_B64, TARGET_VALIDATOR
from harness.evidence import prometheus_maxima, scan_evidence
from harness.jsonio import write_json
from harness.monitor import load_monitor_samples, summarize_monitor
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


MAX_MONITOR_GAP_MS = 5000


def monitor_report(samples: list, fault_ms: int, finish_ms: int) -> dict:
    """Monitor summary plus whether it observed the whole interval from the first fault to the session finish.

    Without samples the dual-signer count was never measured, so it is None
    rather than 0. observed_problems lists every reason the coverage is not complete.
    """
    summary = summarize_monitor(samples, TARGET_VALIDATOR)
    problems = []
    good = [sample for sample in samples if sample.error is None]
    if not good:
        problems.append("no monitor samples")
        summary["dual_signer_seconds"] = None
        summary["indeterminate_seconds"] = None
    else:
        if good[0].t_ms > fault_ms + MAX_MONITOR_GAP_MS:
            problems.append(f"first sample {good[0].t_ms - fault_ms} ms after the first fault")
        if good[-1].t_ms < finish_ms - MAX_MONITOR_GAP_MS:
            problems.append(f"last sample {finish_ms - good[-1].t_ms} ms before finish")
        if summary["max_gap_ms"] > MAX_MONITOR_GAP_MS:
            problems.append(f"gap of {summary['max_gap_ms']} ms between samples")
    if summary["errors"]:
        problems.append(f"{summary['errors']} sampling errors")
    summary["observed"] = not problems
    summary["observed_problems"] = problems
    return summary


def collect_safety(env, session) -> dict:
    """Write safety.json: scan forward from the session's first fault height to the readable tip, and more."""
    events = read_events(session.directory / "events.jsonl")
    faults = [event for event in events if event["kind"] == "fault_injected"]
    if not faults:
        raise ValueError(f"session {session.id} has no fault injection to scan from")
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
        "monitor": monitor_report(monitor_samples, earliest["t_ms"], now_ms),
        "logs": copy_logs(env.paths, session.directory),
    }
    write_json(session.directory / "safety.json", safety)
    session.events.append("finish", evidence_first=first, evidence_last=last, evidence_found=len(scan.evidence))
    return safety
