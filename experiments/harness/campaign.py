"""Campaign-wide evidence scan: one forward scan from the series session's first fault to the chain tip.

The per-session scans of finish stop at each session's own finish time. The campaign spans several
sessions and days, so this scan covers the whole range once and states how it relates to the evidence
max age, which decides whether an equivocation could still have been committed. The relation is given
from the first fault and from the end of the session that holds the campaign's last fault (IV-10): a
double sign can happen until the failback of the last fault, above the fault height, so only a tip
beyond both bounds counted from the end of that session shows that no fault of the campaign can still
have its evidence committed.
"""

from dataclasses import asdict
from pathlib import Path

from harness.evidence import passes_max_age, scan_evidence
from harness.finish import first_fault_height, latest_fault
from harness.jsonio import read_json, write_json
from harness.session import SESSION_ID, read_events
from harness.signing import load_heights

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

    covers_max_age is true only when the span passes both bounds (evidence.passes_max_age).
    """
    return {"blocks_beyond_max_age": span_blocks - max_age_num_blocks,
            "seconds_beyond_max_age": span_seconds - max_age_duration_s,
            "covers_max_age": passes_max_age(span_blocks, span_seconds, max_age_num_blocks, max_age_duration_s)}


def campaign_last_fault(data_dir: Path, session_ids: list) -> dict | None:
    """The highest of the sessions' latest faults (finish.latest_fault) as {session, height, t_ms}.

    Sessions without a fault are skipped. ValueError, naming the session, when its latest fault carries no
    height, as first_fault_height refuses an earliest one without it: a coverage counted from a lower height
    would be overstated.
    """
    found = None
    for session_id in session_ids:
        try:
            fault = latest_fault(read_events(data_dir / session_id / "events.jsonl"))
        except ValueError as error:
            raise ValueError(f"session {session_id}: {error}") from error
        if fault is None:
            continue
        if found is None or fault["height"] > found["height"]:
            found = {"session": session_id, "height": fault["height"], "t_ms": fault["t_ms"]}
    return found


def exposure_end(data_dir: Path, session_id: str) -> dict:
    """The end of a session as {height, t_ms}: up to there a fault of the session could still lead to a double sign.

    With safety.json, the tip its evidence scan read at finish and that block's header time; without it,
    the last height of heights.jsonl and that height's header time. Both times are on the container
    clock, like the block times of the campaign scan. ValueError when the session has neither.
    """
    directory = data_dir / session_id
    if (directory / "safety.json").exists():
        scan = read_json(directory / "safety.json")["evidence_scan"]
        return {"height": scan["last_height"], "t_ms": scan["last_block_time_ms"]}
    records = load_heights(directory / "heights.jsonl")
    if not records:
        raise ValueError(f"session {session_id} has neither safety.json nor stored heights to end the exposure "
                         "of its last fault")
    return {"height": records[-1].height, "t_ms": records[-1].header_time_ms}


def scan_campaign(env, from_session: str) -> dict:
    """Scan evidence from from_session's first fault height to the tip and write data/campaign-scan.json.

    Evidence is final at the tip, so the whole range up to it is read. The result also names the
    campaign's last fault (the highest fault height of the sessions from from_session on), the end of
    the session that holds it (exposure_end_height and exposure_end_t_ms), and the relation of the tip
    to the max-age bounds counted from that end. ValueError when the session does not exist, injected
    no fault, has an earliest or latest fault event without a height, or when the last fault's session
    has neither safety.json nor stored heights; every check runs before the scan.
    """
    if SESSION_ID.match(from_session) is None or not (env.paths.data_dir / from_session).is_dir():
        raise ValueError(f"session {from_session} does not exist in {env.paths.data_dir}")
    events = read_events(env.paths.data_dir / from_session / "events.jsonl")
    first = first_fault_height(from_session, events)
    sessions = sessions_from(env.paths.data_dir, from_session)
    last = campaign_last_fault(env.paths.data_dir, sessions)
    end = exposure_end(env.paths.data_dir, last["session"])
    scan = scan_evidence(env.rpc, first, env.rpc.tip_height(), env.rpc.evidence_params())
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
        "exposure_end_height": end["height"],
        "exposure_end_t_ms": end["t_ms"],
        "relation_to_last_fault": relation_to_max_age(scan.last_height - end["height"],
                                                      (scan.last_block_time_ms - end["t_ms"]) / 1000,
                                                      scan.max_age_num_blocks, scan.max_age_duration_s),
    }
    write_json(env.paths.data_dir / CAMPAIGN_SCAN_FILE, result)
    return result
