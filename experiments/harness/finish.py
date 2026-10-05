"""Session end (R7): forward evidence scan, signing info, Prometheus series, monitor summary, log copies."""

import shutil
from dataclasses import asdict
from pathlib import Path

from harness.constants import TARGET_PUBKEY_B64, TARGET_VALIDATOR, TIP_MARGIN
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


def collect_safety(env, session) -> dict:
    """Write safety.json: scan forward from the session's first fault height to the readable tip, and more."""
    events = read_events(session.directory / "events.jsonl")
    fault_heights = [event["height"] for event in events if event["kind"] == "fault_injected" and "height" in event]
    if not fault_heights:
        raise ValueError(f"session {session.id} has no fault injection to scan from")
    first = min(fault_heights)
    last = env.rpc.tip_height() - TIP_MARGIN
    scan = scan_evidence(env.rpc, first, last, env.rpc.evidence_params())
    now_ms = env.clock.now_ms()
    monitor_samples = load_monitor_samples(session.directory / "monitor.jsonl")
    safety = {
        "session": session.id,
        "t_ms": now_ms,
        "evidence_scan": asdict(scan),
        "signing_info": env.story.signing_info(TARGET_PUBKEY_B64),
        "prometheus": prometheus_maxima(env.prometheus, session.metadata["start_ms"] // 1000, now_ms // 1000),
        "monitor": summarize_monitor(monitor_samples, TARGET_VALIDATOR),
        "logs": copy_logs(env.paths, session.directory),
    }
    write_json(session.directory / "safety.json", safety)
    session.events.append("finish", evidence_first=first, evidence_last=last, evidence_found=len(scan.evidence))
    return safety
