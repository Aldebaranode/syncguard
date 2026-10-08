"""scan-campaign: one forward evidence scan from the series session's first fault to the tip, across all sessions."""

import pytest

from fakes import make_session
from harness.__main__ import main
from harness.campaign import CAMPAIGN_SCAN_FILE, scan_campaign
from harness.jsonio import read_json
from test_finish import finish_env


def two_sessions(env):
    """S4 with faults at heights 1185 and 1183, and S5 with a later fault; returns the sessions."""
    first = make_session(env.paths.data_dir, env.clock, "S4")
    first.events.append("fault_injected", height=1185, iteration=2)
    first.events.append("fault_injected", height=1183, iteration=1)
    second = make_session(env.paths.data_dir, env.clock, "S5")
    second.events.append("fault_injected", height=1187, iteration=1)
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
