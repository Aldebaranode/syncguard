"""Campaign-wide evidence scan: one forward scan from the series session's first fault to the chain tip.

The per-session scans of finish stop at each session's own finish time. The campaign spans several
sessions and days, so this scan covers the whole range once and states how it relates to the evidence
max age, which decides whether an equivocation could still have been committed. The relation is given
from the first fault and from the campaign's last fault (IV-10): only a tip beyond both bounds counted
from the last fault shows that no fault of the campaign can still have its evidence committed.
"""

from dataclasses import asdict
from pathlib import Path

from harness.clock import parse_rfc3339_ms
from harness.evidence import scan_evidence
from harness.finish import first_fault_height, last_fault
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


def relation_to_max_age(span_blocks: int, span_seconds: float, max_age_num_blocks: int, max_age_duration_s: float) -> dict:
    """How far a span lies beyond (positive) or short of (negative) each evidence max-age bound.

    covers_max_age is true only when the span passes both bounds, the expiry rule of CometBFT
    (evidence/verify.go IsEvidenceExpired).
    """
    blocks_beyond = span_blocks - max_age_num_blocks
    seconds_beyond = span_seconds - max_age_duration_s
    return {"blocks_beyond_max_age": blocks_beyond, "seconds_beyond_max_age": seconds_beyond,
            "covers_max_age": blocks_beyond > 0 and seconds_beyond > 0}


def campaign_last_fault(data_dir: Path, session_ids: list) -> dict | None:
    """The fault with the highest height over the sessions as {session, height, t_ms}; sessions without a fault are skipped."""
    found = None
    for session_id in session_ids:
        fault = last_fault(session_id, read_events(data_dir / session_id / "events.jsonl"))
        if fault is None:
            continue
        if found is None or fault["height"] > found["height"]:
            found = {"session": session_id, "height": fault["height"], "t_ms": fault["t_ms"]}
    return found


def block_time_ms(rpc, height: int) -> int:
    """Header time of one block in epoch ms (container clock, like the scan's block times)."""
    return parse_rfc3339_ms(rpc.block(height)["block"]["header"]["time"])


def scan_campaign(env, from_session: str) -> dict:
    """Scan evidence from from_session's first fault height to the tip and write data/campaign-scan.json.

    Evidence is final at the tip, so the whole range up to it is read. The result also names the
    campaign's last fault (the highest fault height of the sessions from from_session on) and the
    relation of the tip to the max-age bounds counted from it. ValueError when the session does not
    exist, injected no fault, or has an earliest or latest fault event without a height; both checks
    run before the scan.
    """
    if SESSION_ID.match(from_session) is None or not (env.paths.data_dir / from_session).is_dir():
        raise ValueError(f"session {from_session} does not exist in {env.paths.data_dir}")
    events = read_events(env.paths.data_dir / from_session / "events.jsonl")
    first = first_fault_height(from_session, events)
    sessions = sessions_from(env.paths.data_dir, from_session)
    last = campaign_last_fault(env.paths.data_dir, sessions)
    scan = scan_evidence(env.rpc, first, env.rpc.tip_height(), env.rpc.evidence_params())
    seconds_since_last = (scan.last_block_time_ms - block_time_ms(env.rpc, last["height"])) / 1000
    result = {
        "from_session": from_session,
        "sessions": sessions,
        "t_ms": env.clock.now_ms(),
        "evidence_scan": asdict(scan),
        "relation": relation_to_max_age(scan.span_blocks, scan.span_seconds, scan.max_age_num_blocks,
                                        scan.max_age_duration_s),
        "last_fault_session": last["session"],
        "last_fault_height": last["height"],
        "last_fault_t_ms": last["t_ms"],
        "relation_to_last_fault": relation_to_max_age(scan.last_height - last["height"], seconds_since_last,
                                                      scan.max_age_num_blocks, scan.max_age_duration_s),
    }
    write_json(env.paths.data_dir / CAMPAIGN_SCAN_FILE, result)
    return result
