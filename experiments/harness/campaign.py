"""Campaign-wide evidence scan: one forward scan from the series session's first fault to the chain tip.

The per-session scans of finish stop at each session's own finish time. The campaign spans several
sessions and days, so this scan covers the whole range once and states how it relates to the evidence
max age, which decides whether an equivocation from the first fault could still have been committed.
"""

from dataclasses import asdict
from pathlib import Path

from harness.evidence import scan_evidence
from harness.finish import first_fault_height
from harness.jsonio import write_json
from harness.session import SESSION_ID, read_events

CAMPAIGN_SCAN_FILE = "campaign-scan.json"


def sessions_from(data_dir: Path, from_session: str) -> list:
    """Ids of the sessions numbered from_session or higher, in numeric order."""
    start = int(SESSION_ID.match(from_session).group(1))
    found = []
    for child in data_dir.iterdir():
        match = SESSION_ID.match(child.name)
        if child.is_dir() and match and int(match.group(1)) >= start:
            found.append((int(match.group(1)), child.name))
    return [name for _, name in sorted(found)]


def relation_to_max_age(scan) -> dict:
    """How far the scan span lies beyond (positive) or short of (negative) each evidence max-age bound."""
    return {
        "blocks_beyond_max_age": scan.span_blocks - scan.max_age_num_blocks,
        "seconds_beyond_max_age": scan.span_seconds - scan.max_age_duration_s,
        "covers_max_age": scan.covers_max_age,
    }


def scan_campaign(env, from_session: str) -> dict:
    """Scan evidence from from_session's first fault height to the tip and write data/campaign-scan.json.

    Evidence is final at the tip, so the whole range up to it is read. ValueError when the session
    does not exist, injected no fault, or has a fault event without a height.
    """
    if SESSION_ID.match(from_session) is None or not (env.paths.data_dir / from_session).is_dir():
        raise ValueError(f"session {from_session} does not exist in {env.paths.data_dir}")
    events = read_events(env.paths.data_dir / from_session / "events.jsonl")
    first = first_fault_height(from_session, events)
    scan = scan_evidence(env.rpc, first, env.rpc.tip_height(), env.rpc.evidence_params())
    result = {
        "from_session": from_session,
        "sessions": sessions_from(env.paths.data_dir, from_session),
        "t_ms": env.clock.now_ms(),
        "evidence_scan": asdict(scan),
        "relation": relation_to_max_age(scan),
    }
    write_json(env.paths.data_dir / CAMPAIGN_SCAN_FILE, result)
    return result
