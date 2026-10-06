"""Dry run on the recorded smoke-test fixtures: a session built without the localnet.

The smoke test recorded no docker-kill instant, so FIXTURE_FAULT_MS is a
declared input. Nothing produced here is a measurement; the session id SFIX
and its purpose field say so.
"""

import shutil
from pathlib import Path

from harness.adapters import RecordedHttp
from harness.analysis import analyze_session
from harness.clock import format_local, format_utc
from harness.constants import (
    ARCHIVED_SESSIONS_CSV,
    BACKUP_NODE,
    FIXTURES_DIR,
    PRIMARY_CONTAINER,
    PRIMARY_NODE,
    RPC1_URL,
    STORY_API_URL,
)
from harness.jsonio import read_json, write_json
from harness.logparse import parse_log_file
from harness.outputs import write_outputs
from harness.rpc import CometRpc, StoryApi
from harness.scenarios.common import append_log_events
from harness.session import EventLog
from harness.signing import parse_commit, store_heights

FIXTURE_SESSION = "SFIX"
FIXTURE_FAULT_MS = 1791168828000  # 2026-10-05T02:53:48.000Z, declared input
FIXTURE_FAULT_HEIGHT = 127
FIXTURE_UTC_OFFSET = 25200
FIXTURE_ITERATION = 1
FIXTURE_LOGS = (("smoke-active.txt", PRIMARY_NODE), ("smoke-passive.txt", BACKUP_NODE))


class FrozenClock:
    """A clock fixed at one instant; the dry run never waits."""

    def __init__(self, epoch_ms: int):
        """Fix the clock."""
        self.epoch_ms = epoch_ms

    def now_ms(self) -> int:
        """The fixed instant."""
        return self.epoch_ms

    def sleep(self, seconds: float) -> None:
        """Waiting is a bug in a dry run."""
        raise RuntimeError("the dry run never sleeps")


def fixture_metadata(fixtures: Path) -> dict:
    """metadata.json of SFIX, with chain facts read through the same clients a live session uses."""
    http = RecordedHttp({
        RPC1_URL + "/validators": read_json(fixtures / "rpc" / "validators.json"),
        RPC1_URL + "/consensus_params": read_json(fixtures / "rpc" / "consensus_params.json"),
        STORY_API_URL + "/slashing/params": read_json(fixtures / "story" / "slashing_params.json"),
    })
    rpc = CometRpc(http, RPC1_URL)
    story = StoryApi(http, STORY_API_URL)
    start_ms = FIXTURE_FAULT_MS - 60_000
    return {
        "session": FIXTURE_SESSION,
        "purpose": "dry run on recorded smoke-test fixtures; not a measurement",
        "start_ms": start_ms,
        "start_utc": format_utc(start_ms),
        "start_local": format_local(start_ms, FIXTURE_UTC_OFFSET),
        "host_utc_offset_seconds": FIXTURE_UTC_OFFSET,
        "syncguard_describe": "fixture",
        "config_id": "fixture",
        "images": {},
        "validators": rpc.validators(),
        "slashing_params": story.slashing_params(),
        "evidence_params": rpc.evidence_params(),
        "clock_offset": {"container": "fixture", "method": "fixture: no offset applied", "offset_ms": 0.0,
                         "offset_uncertainty_ms": 0.0, "samples_ms": [], "round_trips_ms": []},
        "host": "fixture",
    }


def build_fixture_session(fixtures: Path, data_dir: Path) -> Path:
    """Write SFIX from the fixtures, replacing an earlier copy, and return its directory.

    The two fixture logs are copied into logs/ under their own names, which are the file labels
    of every event source, so excerpts and sources stay checkable in the dry run too. The iteration
    ends after its last recorded log event, so its run window spans the whole cycle.
    """
    directory = data_dir / FIXTURE_SESSION
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    metadata = fixture_metadata(fixtures)
    write_json(directory / "metadata.json", metadata)
    log = EventLog(directory / "events.jsonl", FIXTURE_SESSION, FrozenClock(metadata["start_ms"]))
    log.append("session_start", purpose=metadata["purpose"])
    log.append("iteration_start", scenario="failover", iteration=FIXTURE_ITERATION)
    log.append("fault_injected", t_ms=FIXTURE_FAULT_MS, scenario="failover", iteration=FIXTURE_ITERATION,
               action="kill", container=PRIMARY_CONTAINER,
               command="docker kill validator1-node (declared fixture input)", exit_code=0,
               ended_ms=FIXTURE_FAULT_MS, stderr="", height=FIXTURE_FAULT_HEIGHT)
    log_events = []
    for name, node in FIXTURE_LOGS:
        log_events += parse_log_file(fixtures / "logs" / name, node, FIXTURE_UTC_OFFSET)
        (directory / "logs").mkdir(exist_ok=True)
        shutil.copy2(fixtures / "logs" / name, directory / "logs" / name)
    append_log_events(log, log_events, FIXTURE_ITERATION)
    records = [parse_commit(result) for result in read_json(fixtures / "rpc" / "commits_smoke.json").values()]
    store_heights(directory / "heights.jsonl", FIXTURE_SESSION, records)
    cycle_end_ms = max(event.t_ms for event in log_events)
    log.append("iteration_result", t_ms=cycle_end_ms + 1000, scenario="failover", iteration=FIXTURE_ITERATION,
               outcome="complete")
    log.append("iteration_end", t_ms=cycle_end_ms + 2000, scenario="failover", iteration=FIXTURE_ITERATION,
               outcome="fixture")
    return directory


def run_dry(out_dir: Path, fixtures: Path = FIXTURES_DIR, archived_csv: Path = ARCHIVED_SESSIONS_CSV) -> list:
    """Build SFIX under out_dir/data, analyze it, and write the reports and values.json to out_dir/results."""
    directory = build_fixture_session(fixtures, out_dir / "data")
    analyze_session(directory)
    return write_outputs([directory], out_dir / "results", archived_csv)
