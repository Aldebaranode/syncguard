"""Tenderduty readings: the missed-block gauges of the localnet chain at the start and end of a session (III-11)."""

import pytest

from fakes import READY_NOW, make_session, ready_env
from harness.adapters import HttpResponse, Unreachable
from harness.constants import TENDERDUTY_METRICS_URL
from harness.jsonio import append_jsonl, read_jsonl
from harness.tenderduty import parse_chain_gauges, reading_pair, record_tenderduty_reading

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


def test_a_reading_appends_the_four_gauges_to_the_session(tmp_path):
    """The reading carries the session, the point, the host time of the request, and the four counts."""
    env, session = tenderduty_env(tmp_path, HttpResponse(200, METRICS))
    reading = record_tenderduty_reading(env, session, "start")
    assert reading == {"session": "S11", "point": "start", "t_ms": READY_NOW, "missed_blocks": 3, "signed_blocks": 28800,
                       "proposed_blocks": 7212, "missed_blocks_for_window": 2}
    assert read_jsonl(session.directory / "tenderduty.jsonl") == [reading]
    assert env.http.requests[-1]["url"] == "http://127.0.0.1:28686/metrics"


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
        record_tenderduty_reading(env, session, "end")
    assert not (session.directory / "tenderduty.jsonl").exists()


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
