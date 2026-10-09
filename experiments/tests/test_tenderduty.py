"""Tenderduty (III-11): the missed-block gauges of the localnet chain at the start and end of a session, and the
missed blocks its log names."""

import pytest

from fakes import (
    FIXTURES,
    MOCK_ADDRESS,
    READY_NOW,
    TARGET_ADDRESS,
    FakeClock,
    make_session,
    ready_env,
    signed_commit_answer,
    tenderduty_height_line,
    write_incident_evidence,
    write_incident_heights,
    write_tenderduty_evidence,
)
from harness.adapters import HttpResponse, Unreachable
from harness.clock import format_utc
from harness.constants import TENDERDUTY_METRICS_URL
from harness.jsonio import append_jsonl, read_jsonl
from harness.session import read_events
from harness.signing import load_heights, parse_commit, store_heights
from harness.tenderduty import (
    incident_counts,
    load_readings,
    pair_coverage,
    parse_chain_gauges,
    read_tenderduty_log,
    reading_pair,
    record_tenderduty_reading,
    stored_commits,
)

LABELS = 'chain_id="story-localnet",moniker="validator, one",name="Story localnet"'
METRICS = f"""# HELP tenderduty_missed_blocks Count of blocks missed without seeing a precommit or prevote since tenderduty was started
# TYPE tenderduty_missed_blocks gauge
tenderduty_missed_blocks{{{LABELS}}} 3
tenderduty_missed_blocks{{chain_id="other-chain",moniker="x",name="Other"}} 99
# TYPE tenderduty_signed_blocks gauge
tenderduty_signed_blocks{{{LABELS}}} 28800
tenderduty_proposed_blocks{{{LABELS}}} 7212
tenderduty_missed_blocks_for_window{{{LABELS}}} 2
tenderduty_missed_block_window{{{LABELS}}} 200
tenderduty_time_since_last_block{{{LABELS}}} 2.981
"""


def tenderduty_env(tmp_path, answer) -> tuple:
    """(environment, session S11) where the Tenderduty metrics endpoint gives answer."""
    env = ready_env(tmp_path, extra_http={TENDERDUTY_METRICS_URL: answer})
    return env, make_session(env.paths.data_dir, env.clock, "S11")


def test_parse_keeps_the_samples_of_the_chain_id_only():
    """Samples of another chain_id are ignored; a label value may hold a comma; values are numbers."""
    gauges = parse_chain_gauges(METRICS, "story-localnet")
    assert gauges["tenderduty_missed_blocks"] == 3
    assert gauges["tenderduty_signed_blocks"] == 28800
    assert gauges["tenderduty_time_since_last_block"] == 2.981
    assert "other-chain" not in str(gauges)


def test_a_reading_appends_the_four_gauges_and_the_tip_to_the_session(tmp_path):
    """The reading carries the session, the point, the host time of the request, the four counts, and the chain tip
    read right after the metrics."""
    env, session = tenderduty_env(tmp_path, HttpResponse(200, METRICS))
    reading = record_tenderduty_reading(env, session, "start")["reading"]
    assert reading == {"session": "S11", "point": "start", "t_ms": READY_NOW, "missed_blocks": 3, "signed_blocks": 28800,
                       "proposed_blocks": 7212, "missed_blocks_for_window": 2, "tip_height": 1188}
    assert read_jsonl(session.directory / "tenderduty.jsonl") == [reading]
    assert [request["url"] for request in env.http.requests[-2:]] == ["http://127.0.0.1:28686/metrics",
                                                                      "http://localhost:26657/status"]


@pytest.mark.parametrize("answer, message", [
    (Unreachable("connection refused"), "did not answer"),
    (HttpResponse(503, "unavailable"), "status 503"),
    (HttpResponse(200, METRICS.replace("tenderduty_proposed_blocks", "tenderduty_other_blocks")),
     "tenderduty_proposed_blocks"),
])
def test_no_reading_is_written_without_an_answer_or_with_a_missing_gauge(tmp_path, answer, message):
    """An endpoint that does not answer, an error status, or a missing gauge raise ValueError and write nothing."""
    env, session = tenderduty_env(tmp_path, answer)
    with pytest.raises(ValueError, match=message):
        record_tenderduty_reading(env, session, "start")
    assert not (session.directory / "tenderduty.jsonl").exists()


def start_reading(session, t_ms: int, tip_height: int | None) -> dict:
    """A start line in tenderduty.jsonl: recorded with its tip, or, with tip_height None, written by hand."""
    reading = {"session": session.id, "point": "start", "t_ms": t_ms, "missed_blocks": 1}
    if tip_height is None:
        reading["source"] = "ditulis tangan"
    else:
        reading["tip_height"] = tip_height
    append_jsonl(session.directory / "tenderduty.jsonl", reading)
    return reading


def served_record(height: int):
    """The commit of height that the fake RPC of ready_env serves, as a HeightRecord."""
    return parse_commit(signed_commit_answer([TARGET_ADDRESS, MOCK_ADDRESS])(f"commit?height={height}")["result"])


def test_an_end_reading_needs_a_start_reading(tmp_path):
    """Without a start the change has nothing to count from: refused before Tenderduty is asked, nothing written."""
    env, session = tenderduty_env(tmp_path, HttpResponse(200, METRICS))
    with pytest.raises(ValueError, match="no start reading"):
        record_tenderduty_reading(env, session, "end")
    assert env.http.requests == []
    assert not (session.directory / "tenderduty.jsonl").exists()


def test_an_end_reading_reads_the_missing_edges_of_its_range_into_tenderduty_heights(tmp_path):
    """Start tip 1180, end tip 1188: the pair covers the commits of 1180 to 1187. heights.jsonl holds 1182 to 1185, so
    1180, 1181, 1186, and 1187 are read into tenderduty-heights.jsonl once 1187 lies two below the tip;
    heights.jsonl stays as it was."""
    env, session = tenderduty_env(tmp_path, HttpResponse(200, METRICS))
    start_reading(session, READY_NOW - 60_000, 1180)
    store_heights(session.directory / "heights.jsonl", "S11", [served_record(height) for height in range(1182, 1186)])
    stored = (session.directory / "heights.jsonl").read_bytes()
    result = record_tenderduty_reading(env, session, "end")
    assert result["reading"]["tip_height"] == 1188
    assert result["commit_range"] == {"first_height": 1180, "last_height": 1187, "heights_read": 4}
    read = [record.height for record in load_heights(session.directory / "tenderduty-heights.jsonl")]
    assert read == [1180, 1181, 1186, 1187]
    assert (session.directory / "heights.jsonl").read_bytes() == stored
    collected = [row for row in read_events(session.directory / "events.jsonl") if row["kind"] == "heights_collected"]
    assert [(row["first"], row["last"], row["file"]) for row in collected] == [
        (1180, 1181, "tenderduty-heights.jsonl"), (1186, 1187, "tenderduty-heights.jsonl")]


def test_an_end_reading_waits_for_a_chain_that_stops_and_then_refuses(tmp_path):
    """A tip that stays at the end reading's own tip never makes its last covered commit readable: ValueError after
    the wait, with the reading kept and nothing read."""
    env, session = tenderduty_env(tmp_path, HttpResponse(200, METRICS))
    env.http.answers["http://localhost:26657/status"] = {"result": {"sync_info": {"latest_block_height": "1188"}}}
    start_reading(session, READY_NOW - 60_000, 1180)
    with pytest.raises(ValueError, match="did not pass height 1187"):
        record_tenderduty_reading(env, session, "end")
    assert [reading["point"] for reading in read_jsonl(session.directory / "tenderduty.jsonl")] == ["start", "end"]
    assert not (session.directory / "tenderduty-heights.jsonl").exists()


def container_block_ms(height: int) -> int:
    """Header time of a height on the containers' clock in the fake RPC of ready_env: 3 s per height."""
    return 1791169000000 + height * 3000


def steady_blocks(url: str) -> dict:
    """A /block answer whose header time follows container_block_ms."""
    height = int(url.rsplit("=", 1)[1])
    block = {"header": {"height": str(height), "time": format_utc(container_block_ms(height))},
             "evidence": {"evidence": []}}
    return {"jsonrpc": "2.0", "id": -1, "result": {"block": block}}


def test_an_end_reading_resolves_a_hand_written_start_by_header_time(tmp_path):
    """The start was written by hand with only t_ms, 1 s before block 1180's header time on the container clock. The
    containers run 2 s ahead of the host, so on the host clock 1181 is the first block at or after it: 1181 becomes
    the start tip in a start_tip_resolved line, and the hand-written line stays exactly as written. A later end
    reading keeps that tip and reads only the heights that no file holds yet."""
    clock = FakeClock(container_block_ms(1190))
    env = ready_env(tmp_path, clock=clock, extra_http={TENDERDUTY_METRICS_URL: HttpResponse(200, METRICS),
                                                        "http://localhost:26657/block?height=*": steady_blocks})
    session = make_session(env.paths.data_dir, env.clock, "S11", offset_ms=2000.0)
    hand = start_reading(session, container_block_ms(1180) - 1000, None)
    written = (session.directory / "tenderduty.jsonl").read_text()
    result = record_tenderduty_reading(env, session, "end")
    assert result["commit_range"] == {"first_height": 1181, "last_height": 1187, "heights_read": 7}
    lines = read_jsonl(session.directory / "tenderduty.jsonl")
    assert (session.directory / "tenderduty.jsonl").read_text().startswith(written)
    assert lines[0] == hand
    assert lines[-1] == {"session": "S11", "point": "start_tip_resolved", "start_t_ms": hand["t_ms"], "tip_height": 1181,
                         "t_ms": container_block_ms(1190)}
    read = [record.height for record in load_heights(session.directory / "tenderduty-heights.jsonl")]
    assert read == list(range(1181, 1188))
    clock.sleep(10)
    again = record_tenderduty_reading(env, session, "end")
    assert again["commit_range"] == {"first_height": 1181, "last_height": 1190, "heights_read": 3}
    resolved = [line for line in read_jsonl(session.directory / "tenderduty.jsonl") if line["point"] == "start_tip_resolved"]
    assert len(resolved) == 1


def test_the_point_is_start_or_end(tmp_path):
    """Any other point is refused before the endpoint is asked."""
    env, session = tenderduty_env(tmp_path, HttpResponse(200, METRICS))
    with pytest.raises(ValueError, match="start or end"):
        record_tenderduty_reading(env, session, "middle")
    assert env.http.requests == []


def test_the_pair_is_the_latest_start_and_the_latest_end_after_it(tmp_path):
    """A start written by hand with a source field counts; an end before the latest start does not."""
    path = tmp_path / "tenderduty.jsonl"
    for point, t_ms in (("start", 1_000), ("end", 1_500), ("start", 2_000), ("end", 5_000), ("end", 9_000)):
        append_jsonl(path, {"point": point, "t_ms": t_ms, "missed_blocks": t_ms // 1000})
    append_jsonl(path, {"point": "start", "t_ms": 3_000, "missed_blocks": 0, "source": "ditulis tangan"})
    start, end = reading_pair(read_jsonl(path))
    assert (start["t_ms"], start["source"], end["t_ms"]) == (3_000, "ditulis tangan", 9_000)
    assert reading_pair([{"point": "start", "t_ms": 5_000}, {"point": "end", "t_ms": 4_000}]) is None
    assert reading_pair([{"point": "end", "t_ms": 4_000}]) is None


def pair_of(directory) -> tuple:
    """(readings, start, end): the readings of a session directory and its reading pair."""
    readings = load_readings(directory)
    return (readings, *reading_pair(readings))


def test_the_pair_coverage_lists_the_heights_of_the_covered_range_without_a_stored_commit(tmp_path):
    """Tips 101 and 112 cover 101 to 111. heights.jsonl holds 100 to 109 and tenderduty-heights.jsonl 111, so 110 has
    no commit; a start written by hand without a resolved tip leaves the coverage unknown."""
    write_tenderduty_evidence(tmp_path / "recorded", "S10", end_tip=112)
    append_jsonl(tmp_path / "recorded" / "tenderduty-heights.jsonl", tenderduty_height_line("S10", 111))
    assert pair_coverage(tmp_path / "recorded", *pair_of(tmp_path / "recorded")) == (101, 111, [110])
    write_tenderduty_evidence(tmp_path / "hand", "S10", start_tip=None)
    assert pair_coverage(tmp_path / "hand", *pair_of(tmp_path / "hand")) is None


LOG_EXCERPT = FIXTURES / "tenderduty" / "tenderduty-run.txt"  # lines copied from data/tenderduty/tenderduty-run.log
# Lines in the shape of that log, for a chain the harness does not read.
OTHER_CHAIN = """2026/10/09 20:37:36 tenderduty |  ⚙️ other-chain  watching for NewBlock and Vote events via tcp://localhost:36657
2026/10/09 20:37:37 tenderduty |  🧊 other-chain  block 20
2026/10/09 20:37:37 tenderduty |  ❌ warning      other-validator missed block 14870 on other-chain
"""


def test_the_log_gives_the_missed_blocks_and_the_watched_blocks_of_the_localnet_chain():
    """The excerpt of the campaign's log names 13 missed blocks of story-localnet in three subscriptions: one until
    Tenderduty restarted at 14:35:57, one until the websocket exited at 17:25:10, and the one from 17:25:35. Each
    spans the first to the last block its progress and missed-block lines name; startup, health, and error lines
    are ignored."""
    log = read_tenderduty_log(LOG_EXCERPT)
    assert log.reason is None
    assert sorted(log.missed) == [8632, 11451, *range(14860, 14867), 14888, 14889, 14890, 15072]
    assert log.watched == ((8000, 8260), (8300, 11451), (11480, 15080))


def test_lines_of_another_chain_are_ignored(tmp_path):
    """A subscription, a progress line, and a missed block of another chain in the middle of the localnet's last
    subscription change neither its missed blocks nor its spans."""
    lines = LOG_EXCERPT.read_text(encoding="utf-8").splitlines(keepends=True)
    path = tmp_path / "tenderduty-run.log"
    path.write_text("".join(lines[:72]) + OTHER_CHAIN + "".join(lines[72:]), encoding="utf-8")
    assert read_tenderduty_log(path) == read_tenderduty_log(LOG_EXCERPT)


def test_a_block_range_is_covered_only_inside_one_subscription():
    """14859 to 14890 lie inside the subscription from 17:25:35. The blocks after 11451 and before 11480 fall in the
    outage around the websocket exit, so no range that touches them is covered, nor one that starts before the
    first block the log names."""
    log = read_tenderduty_log(LOG_EXCERPT)
    assert log.covers(14859, 14890)
    assert log.covers(8300, 11451)
    assert not log.covers(11452, 11479)
    assert not log.covers(11440, 11500)
    assert not log.covers(7990, 8010)


def test_a_missing_log_reads_as_empty_with_the_reason(tmp_path):
    """No file, or no data directory to look in: no missed block, nothing covered, and the reason for the caller."""
    for path in (tmp_path / "tenderduty-run.log", None):
        log = read_tenderduty_log(path)
        assert (log.missed, log.watched, log.reason) == (
            frozenset(), (), "tenderduty/tenderduty-run.log was not found in the data directory")
        assert not log.covers(14859, 14890)


def test_an_incident_counts_the_same_misses_in_the_log_and_on_the_chain(tmp_path):
    """S10's incident, from the fault at 14858 to the primary's first signature after failback at 14890: of the 13
    missed blocks of the log, 14860 to 14866 and 14888 to 14890 report the commits 14859 to 14865 and 14887 to
    14889, where 48DC2183 is absent; 8632, 11451, and 15072 lie outside the incident. The nil vote at 14875 is
    present on both sides: the log has no missed block 14876 and the chain does not count 14875 as absent."""
    write_incident_evidence(tmp_path, "S10")
    missed = read_tenderduty_log(LOG_EXCERPT).missed
    counts = incident_counts(stored_commits(tmp_path), missed, 14858, 14890, TARGET_ADDRESS)
    assert counts == {"logged_missed": 10, "missing_heights": 0, "chain_absent": 10, "absent_on_proposed": 0,
                      "difference": 0}


def test_tenderduty_reports_the_commit_of_h_minus_1_at_both_edges(tmp_path):
    """Fault at 100, first signature at 110: the chain counts the commits of 100 to 109 and the log the blocks 101 to
    110. 48DC2183 absent at 99, 100, 109, and 110 shows in the log as 100, 101, 110, and 111; at each edge the pair
    inside counts (100 with 101, 109 with 110) and the pair outside does not (99 with 100, 110 with 111)."""
    write_incident_heights(tmp_path, "S10", 95, 115, dict.fromkeys((99, 100, 109, 110), "absent"))
    counts = incident_counts(stored_commits(tmp_path), frozenset({100, 101, 110, 111}), 100, 110, TARGET_ADDRESS)
    assert (counts["logged_missed"], counts["chain_absent"], counts["difference"]) == (2, 2, 0)


def test_an_absent_height_before_a_block_48dc2183_proposed_is_counted_apart(tmp_path):
    """48DC2183 absent at 103 and 104 and the proposer of block 105: Tenderduty reports 105 as proposed whatever its
    LastCommit holds, so the log names 104 only. The chain counts 2 absent heights, 1 of them before a block
    48DC2183 proposed, and Selisih compares the log with the other 1."""
    write_incident_heights(tmp_path, "S10", 95, 115, dict.fromkeys((103, 104), "absent"), {105: TARGET_ADDRESS})
    counts = incident_counts(stored_commits(tmp_path), frozenset({104}), 100, 110, TARGET_ADDRESS)
    assert counts == {"logged_missed": 1, "missing_heights": 0, "chain_absent": 2, "absent_on_proposed": 1,
                      "difference": 0}


def test_the_chain_counts_stay_empty_while_a_height_of_the_incident_has_no_commit(tmp_path):
    """Heights 95 to 109 only: the header of 110 names the proposer of the last block the log reports, so 1 height
    lacks commit data and the chain counts and Selisih stay empty; the log count does not need the chain."""
    write_incident_heights(tmp_path, "S10", 95, 109, dict.fromkeys((103,), "absent"))
    counts = incident_counts(stored_commits(tmp_path), frozenset({104}), 100, 110, TARGET_ADDRESS)
    assert counts == {"logged_missed": 1, "missing_heights": 1, "chain_absent": None, "absent_on_proposed": None,
                      "difference": None}
