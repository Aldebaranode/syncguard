"""Typed reads from CometBFT RPC, the Story API, and Prometheus."""

import pytest

from fakes import FakeHttp, load_fixture
from harness.adapters import HttpResponse, Unreachable
from harness.constants import TARGET_PUBKEY_B64, TARGET_VALIDATOR
from harness.rpc import CometRpc, Prometheus, StoryApi, pubkey_hex_from_base64

TARGET_PUBKEY_HEX = "0x02f2e8fa5920d145a8ce823ea49cefd5aeeea6df8768cc5909d5a0471bd9f026f5"


def rpc_with(base: str, path: str, fixture: str) -> CometRpc:
    """A CometRpc whose single endpoint answers with a recorded fixture."""
    return CometRpc(FakeHttp({base + path: load_fixture(fixture)}), base)


def test_tip_height_from_status():
    """latest_block_height is read as an integer."""
    assert rpc_with("http://localhost:26657", "/status", "rpc/status_rpc1.json").tip_height() == 1188


def test_loaded_address_names_the_key_each_node_runs():
    """validator1-node loaded 48DC2183, validator2-node a mock key."""
    primary = rpc_with("http://localhost:21657", "/status", "rpc/status_validator1.json")
    backup = rpc_with("http://localhost:22657", "/status", "rpc/status_validator2.json")
    assert primary.loaded_address() == TARGET_VALIDATOR
    assert backup.loaded_address() == "E6FAA3F2B3E9158BA9BF93BE1F7F8D3095EAD04D"


def test_validators_with_equal_power():
    """Four bonded validators, power 1 each."""
    validators = rpc_with("http://localhost:26657", "/validators", "rpc/validators.json").validators()
    assert [validator["address"][:8] for validator in validators] == ["3DD8CBA0", "48DC2183", "7EFAECCD", "EE8E45BA"]
    assert {validator["voting_power"] for validator in validators} == {1}
    assert validators[1]["pub_key"] == TARGET_PUBKEY_B64


def test_evidence_params_in_seconds():
    """max_age_duration arrives in nanoseconds."""
    rpc = rpc_with("http://localhost:26657", "/consensus_params", "rpc/consensus_params.json")
    assert rpc.evidence_params() == {"max_age_num_blocks": 100000, "max_age_duration_s": 172800.0}


def test_block_carries_an_evidence_list():
    """block.evidence.evidence is a list."""
    rpc = rpc_with("http://localhost:26657", "/block?height=1183", "rpc/block_1183.json")
    assert rpc.block(1183)["block"]["evidence"]["evidence"] == []


def test_block_time_is_the_header_time_in_epoch_ms():
    """Block 1183 has header time 03:57:37.874418465Z; the nanoseconds are cut to the millisecond."""
    rpc = rpc_with("http://localhost:26657", "/block?height=1183", "rpc/block_1183.json")
    assert rpc.block_time_ms(1183) == 1791172657874


def test_answer_without_result_is_unreachable():
    """A JSON-RPC error answer is not silently accepted."""
    rpc = CometRpc(FakeHttp({"http://x/status": {"error": "boom"}}), "http://x")
    with pytest.raises(Unreachable):
        rpc.status()


def test_pubkey_hex_form():
    """Story's API wants the 33-byte compressed key as 0x-hex."""
    assert pubkey_hex_from_base64(TARGET_PUBKEY_B64) == TARGET_PUBKEY_HEX


def test_signing_info_path_and_omitted_fields():
    """tombstoned and start_height are omitted by amino JSON when false or zero."""
    url = "http://localhost:1317/slashing/signing_infos/" + TARGET_PUBKEY_HEX
    http = FakeHttp({url: load_fixture("story/signing_info.json")})
    info = StoryApi(http, "http://localhost:1317").signing_info(TARGET_PUBKEY_B64)
    assert http.requests[0]["url"] == url
    assert info == {
        "url_path": "/slashing/signing_infos/" + TARGET_PUBKEY_HEX,
        "cons_address": "storyvalcons1frwzrqunlnh0263hm93msp86hykx9j5a9wtwrv",
        "missed_blocks_counter": 3,
        "jailed_until": "1970-01-01T00:00:00Z",
        "tombstoned": False,
        "index_offset": 1283,
        "start_height": 0,
    }


def test_slashing_params_as_numbers():
    """Window, minimum share, and jail duration in seconds."""
    http = FakeHttp({"http://localhost:1317/slashing/params": load_fixture("story/slashing_params.json")})
    params = StoryApi(http, "http://localhost:1317").slashing_params()
    assert params["signed_blocks_window"] == 200
    assert params["min_signed_per_window"] == 0.05
    assert params["downtime_jail_duration_s"] == 60.0
    assert params["slash_fraction_double_sign"] == 0.05


def test_story_error_code_is_unreachable():
    """The answer the base64 path produced on 2026-10-05."""
    http = FakeHttp({"http://localhost:1317/slashing/params": {"code": 500, "msg": None, "error": "hex string without 0x prefix"}})
    with pytest.raises(Unreachable, match="hex string"):
        StoryApi(http, "http://localhost:1317").slashing_params()


def test_prometheus_range_series():
    """Each series becomes its instance label and numeric samples."""
    http = FakeHttp({"http://localhost:9090/api/v1/query_range*": load_fixture("prometheus/byzantine_range.json")})
    series = Prometheus(http, "http://localhost:9090").query_range(
        "cometbft_consensus_byzantine_validators", 1791172785, 1791172845, 15)
    assert [item["instance"] for item in series] == ["validator1-node:26660", "validator2-node:26660"]
    assert series[0]["values"][0] == (1791172785, 0.0)
    assert "query=cometbft_consensus_byzantine_validators" in http.requests[0]["url"]


def test_prometheus_readiness():
    """Ready answers 200; an unreachable server is not up."""
    ready = FakeHttp({"http://localhost:9090/-/ready": HttpResponse(200, "Prometheus Server is Ready.\n")})
    down = FakeHttp({"http://localhost:9090/-/ready": Unreachable("down")})
    assert Prometheus(ready, "http://localhost:9090").is_up() is True
    assert Prometheus(down, "http://localhost:9090").is_up() is False
