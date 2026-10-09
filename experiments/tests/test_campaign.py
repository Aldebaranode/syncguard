"""scan-campaign: one forward evidence scan from the series session's first fault to the tip, across all sessions."""

from types import SimpleNamespace

import pytest

from fakes import FakeClock, make_session, paths_under
from harness.__main__ import main
from harness.campaign import CAMPAIGN_SCAN_FILE, scan_campaign
from harness.clock import format_utc, parse_rfc3339_ms
from harness.evidence import EvidenceScan, passes_max_age
from harness.jsonio import read_json, write_json
from harness.session import open_session
from harness.signing import HeightRecord, store_heights
from test_finish import finish_env


def finished_at(session, height: int, block_time_ms: int) -> None:
    """A safety.json whose evidence scan read the tip height at finish, with that block's header time."""
    write_json(session.directory / "safety.json", {"session": session.id, "evidence_scan": {
        "last_height": height, "last_block_time_ms": block_time_ms}})


def two_sessions(env):
    """S4 with faults at heights 1185 and 1183, and S5 with a later fault whose finish read the tip 1188; returns both."""
    first = make_session(env.paths.data_dir, env.clock, "S4")
    first.events.append("fault_injected", height=1185, iteration=2)
    first.events.append("fault_injected", height=1183, iteration=1)
    second = make_session(env.paths.data_dir, env.clock, "S5")
    second.events.append("fault_injected", height=1187, iteration=1)
    finished_at(second, 1188, parse_rfc3339_ms("2026-10-05T03:57:38.000Z"))
    return first, second


def test_scan_campaign_reads_from_the_first_fault_of_the_named_session_to_the_tip(tmp_path):
    """1183..1188, the sessions from S4 on, and the relation to the evidence max age."""
    env = finish_env(tmp_path)
    two_sessions(env)
    make_session(env.paths.data_dir, env.clock, "S3")
    result = scan_campaign(env, "S4")
    scan = result["evidence_scan"]
    assert (scan["first_height"], scan["last_height"], scan["evidence"]) == (1183, 1188, [])
    assert result["from_session"] == "S4" and result["sessions"] == ["S4", "S5"]
    relation = result["relation"]
    assert relation["blocks_beyond_max_age"] == scan["span_blocks"] - scan["max_age_num_blocks"] < 0
    assert relation["seconds_beyond_max_age"] == pytest.approx(scan["span_seconds"] - scan["max_age_duration_s"])
    assert relation["covers_max_age"] is False
    assert read_json(env.paths.data_dir / CAMPAIGN_SCAN_FILE) == result


LAST_FAULT_HEIGHT = 40_000
SESSION_END_HEIGHT = 40_031  # the tip S11's finish read, 31 blocks after its last fault
LAST_FAULT_BLOCK_MS = 1_791_500_000_000
FORTY_NINE_HOURS_MS = 49 * 3_600_000


def steady_block_ms(height: int) -> int:
    """Header time of a height on the steady chain: 100.100 blocks take 49 h, counted from the block of 40.000."""
    return LAST_FAULT_BLOCK_MS + round((height - LAST_FAULT_HEIGHT) * FORTY_NINE_HOURS_MS / 100_100)


class SteadyChain:
    """A chain without evidence whose blocks follow each other evenly: 100.100 blocks take 49 h, so tip 140.100 is
    49 h after 40.000. It answers the campaign scan's reads without HTTP."""

    def __init__(self, tip: int):
        """Fix the tip height."""
        self.tip = tip

    def tip_height(self) -> int:
        """The tip height."""
        return self.tip

    def evidence_params(self) -> dict:
        """The localnet's evidence max age: 100.000 blocks and 48 h."""
        return {"max_age_num_blocks": 100_000, "max_age_duration_s": 172_800.0}

    def block(self, height: int) -> dict:
        """A block without evidence whose header time keeps the steady block interval."""
        return {"block": {"header": {"time": format_utc(steady_block_ms(height))}, "evidence": {"evidence": []}}}


def end_block_scan(rpc, first_height: int, last_height: int, evidence_params: dict) -> EvidenceScan:
    """Stand-in for scan_evidence on the evidence-free steady chain: its two end blocks give the whole result.

    Reading the 139.288 blocks one by one takes seconds and adds nothing to the scan tests of test_evidence.py
    and the scan-campaign test above, which run the real scan.
    """
    first_ms = parse_rfc3339_ms(rpc.block(first_height)["block"]["header"]["time"])
    last_ms = parse_rfc3339_ms(rpc.block(last_height)["block"]["header"]["time"])
    span_blocks = last_height - first_height
    span_seconds = (last_ms - first_ms) / 1000
    max_blocks, max_seconds = evidence_params["max_age_num_blocks"], evidence_params["max_age_duration_s"]
    return EvidenceScan(first_height, last_height, span_blocks + 1, [], first_ms, last_ms, span_blocks, span_seconds,
                        max_blocks, max_seconds, passes_max_age(span_blocks, span_seconds, max_blocks, max_seconds))


def chain_env(tmp_path, tip: int, monkeypatch, with_safety: bool = True):
    """The data directory of S4 (faults at 813 and 1.200), S6 (no fault), and S11 (fault at 40.000) on a steady chain.

    S11 ends at 40.031: the tip its finish read in safety.json, or with with_safety False the last height of its
    heights.jsonl.
    """
    monkeypatch.setattr("harness.campaign.scan_evidence", end_block_scan)
    clock = FakeClock(1_791_700_000_000)
    env = SimpleNamespace(paths=paths_under(tmp_path), rpc=SteadyChain(tip), clock=clock)
    series = make_session(env.paths.data_dir, clock, "S4")
    series.events.append("fault_injected", t_ms=1_000, height=813, iteration=1)
    series.events.append("fault_injected", t_ms=2_000, height=1_200, iteration=2)
    make_session(env.paths.data_dir, clock, "S6").events.append("security_negatives", rows=8)
    last = make_session(env.paths.data_dir, clock, "S11")
    last.events.append("fault_injected", t_ms=9_000, height=LAST_FAULT_HEIGHT, iteration=1)
    if with_safety:
        finished_at(last, SESSION_END_HEIGHT, steady_block_ms(SESSION_END_HEIGHT))
    else:
        records = [HeightRecord(height, steady_block_ms(height), "P", 0, {}, {}, 0)
                   for height in (SESSION_END_HEIGHT, SESSION_END_HEIGHT - 1)]
        store_heights(last.directory / "heights.jsonl", "S11", records)
    return env


def test_the_campaign_scan_states_its_coverage_from_the_end_of_the_last_fault_session(tmp_path, monkeypatch):
    """Tip 140.100: the last fault is at 40.000 in S11, whose finish read the tip 40.031; both bounds count from 40.031.

    A double sign can happen until the failback of the last fault, so the exposure ends with its session.
    """
    result = scan_campaign(chain_env(tmp_path, 140_100, monkeypatch), "S4")
    assert (result["evidence_scan"]["first_height"], result["evidence_scan"]["last_height"]) == (813, 140_100)
    assert (result["last_fault_session"], result["last_fault_height"], result["last_fault_t_ms"]) == ("S11", 40_000, 9_000)
    assert (result["exposure_end_height"], result["exposure_end_t_ms"]) == (40_031, steady_block_ms(40_031))
    relation = result["relation_to_last_fault"]
    assert relation["blocks_beyond_max_age"] == 69
    expected_s = (steady_block_ms(140_100) - steady_block_ms(40_031)) / 1000 - 172_800
    assert relation["seconds_beyond_max_age"] == pytest.approx(expected_s, abs=0.001)
    assert relation["covers_max_age"] is True
    assert result["relation"]["covers_max_age"] is True


@pytest.mark.parametrize("tip, blocks_beyond, covered", [(140_020, -11, False), (140_040, 9, True)])
def test_coverage_counts_from_the_session_end_not_from_the_fault(tmp_path, monkeypatch, tip, blocks_beyond, covered):
    """Fault at 40.000, session end at 40.031: tip 140.020 lies 100.020 blocks after the fault but 11 short of the bound
    from the session end, so it does not cover; tip 140.040, about 49 h after the session end, passes both bounds."""
    relation = scan_campaign(chain_env(tmp_path, tip, monkeypatch), "S4")["relation_to_last_fault"]
    assert relation["blocks_beyond_max_age"] == blocks_beyond
    assert relation["covers_max_age"] is covered


def test_a_last_fault_session_without_safety_ends_at_its_last_stored_height(tmp_path, monkeypatch):
    """No safety.json in S11: its exposure ends at the last height of heights.jsonl (40.031) and that header time."""
    result = scan_campaign(chain_env(tmp_path, 140_040, monkeypatch, with_safety=False), "S4")
    assert (result["exposure_end_height"], result["exposure_end_t_ms"]) == (40_031, steady_block_ms(40_031))
    assert result["relation_to_last_fault"]["blocks_beyond_max_age"] == 9


def test_a_last_fault_session_without_safety_or_heights_is_refused(tmp_path, monkeypatch):
    """Neither file in S11: the end of its exposure is unknown, so the scan is refused before it runs."""
    env = chain_env(tmp_path, 140_040, monkeypatch, with_safety=False)
    (env.paths.data_dir / "S11" / "heights.jsonl").unlink()
    with pytest.raises(ValueError, match="neither safety.json nor stored heights"):
        scan_campaign(env, "S4")
    assert not (env.paths.data_dir / CAMPAIGN_SCAN_FILE).exists()


def test_a_tip_short_of_the_block_bound_from_the_last_fault_is_not_covered(tmp_path, monkeypatch):
    """Tip 139.000: more than 48 h after the session end at 40.031 but 1.031 blocks short of 100.000, so not covered."""
    relation = scan_campaign(chain_env(tmp_path, 139_000, monkeypatch), "S4")["relation_to_last_fault"]
    assert relation["blocks_beyond_max_age"] == -1_031
    expected_s = (steady_block_ms(139_000) - steady_block_ms(40_031)) / 1000 - 172_800
    assert relation["seconds_beyond_max_age"] == pytest.approx(expected_s, abs=0.001)
    assert relation["seconds_beyond_max_age"] > 0
    assert relation["covers_max_age"] is False


def test_the_last_fault_needs_a_height(tmp_path, monkeypatch):
    """A session whose latest fault event has no height cannot bound the coverage: refused like the first fault."""
    env = chain_env(tmp_path, 140_100, monkeypatch)
    open_session(env.paths.data_dir, "S11", env.clock).events.append("fault_injected", t_ms=9_500, iteration=2)
    with pytest.raises(ValueError, match="latest fault_injected event has no height"):
        scan_campaign(env, "S4")


def test_scan_campaign_needs_a_fault_in_the_named_session(tmp_path):
    """A session without fault injection has no height to scan from; nothing is written."""
    env = finish_env(tmp_path)
    make_session(env.paths.data_dir, env.clock, "S4")
    with pytest.raises(ValueError, match="no fault injection"):
        scan_campaign(env, "S4")
    assert not (env.paths.data_dir / CAMPAIGN_SCAN_FILE).exists()


def test_scan_campaign_command_delegates_and_prints(tmp_path, capsys):
    """The CLI command runs the domain function and prints its result; an unknown session exits 1."""
    env = finish_env(tmp_path)
    two_sessions(env)
    assert main(["scan-campaign", "--from-session", "S4"], env_factory=lambda: env) == 0
    assert '"from_session": "S4"' in capsys.readouterr().out
    assert main(["scan-campaign", "--from-session", "S9"], env_factory=lambda: env) == 1
