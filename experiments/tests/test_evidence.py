"""Forward evidence scan and Prometheus safety series."""

from urllib.parse import unquote

import pytest

from fakes import FakeHttp, block_answers, load_fixture
from harness.adapters import Unreachable
from harness.evidence import prometheus_maxima, scan_evidence
from harness.rpc import CometRpc, Prometheus

PARAMS = {"max_age_num_blocks": 100000, "max_age_duration_s": 172800.0}
DUPLICATE_VOTE = {"type": "tendermint/DuplicateVoteEvidence", "value": {}}  # type name from cometbft types/evidence.go


def rpc_for(answer) -> CometRpc:
    """A CometRpc serving /block from one answer."""
    return CometRpc(FakeHttp({"http://localhost:26657/block?height=*": answer}), "http://localhost:26657")


def five_blocks():
    """Heights 100-104, 3 s apart, with two duplicate-vote items in 102 (synthetic)."""
    times = {100 + i: f"2026-10-05T03:00:{3 * i:02d}.000Z" for i in range(5)}
    return block_answers(times, {102: [DUPLICATE_VOTE, DUPLICATE_VOTE]})


def test_scan_of_a_recorded_block():
    """The recorded block 1183 has an empty evidence list."""
    answer = {"jsonrpc": "2.0", "id": -1, "result": load_fixture("rpc/block_1183.json")["result"]}
    scan = scan_evidence(rpc_for(answer), 1183, 1183, PARAMS)
    assert (scan.heights_scanned, scan.evidence, scan.covers_max_age) == (1, [], False)
    assert scan.first_block_time_ms == scan.last_block_time_ms == 1791172657874


def test_scan_reports_evidence_heights_and_span():
    """Evidence at 102 is found; the scan spans 4 blocks and 12 s."""
    scan = scan_evidence(rpc_for(five_blocks()), 100, 104, PARAMS)
    assert scan.evidence == [{"height": 102, "count": 2, "types": ["tendermint/DuplicateVoteEvidence"]}]
    assert (scan.first_height, scan.last_height, scan.heights_scanned) == (100, 104, 5)
    assert (scan.span_blocks, scan.span_seconds) == (4, 12.0)


def test_max_age_coverage_needs_both_limits():
    """CometBFT expires evidence only when both ages are exceeded."""
    both = scan_evidence(rpc_for(five_blocks()), 100, 104, {"max_age_num_blocks": 3, "max_age_duration_s": 10.0})
    blocks_only = scan_evidence(rpc_for(five_blocks()), 100, 104, {"max_age_num_blocks": 3, "max_age_duration_s": 20.0})
    assert both.covers_max_age is True
    assert blocks_only.covers_max_age is False


def test_empty_range_is_refused():
    """A scan must cover at least the fault height."""
    with pytest.raises(ValueError):
        scan_evidence(rpc_for(five_blocks()), 104, 100, PARAMS)


def test_prometheus_maxima_when_up_and_down():
    """Maximum per instance when Prometheus answers; a flag when it does not."""
    up = FakeHttp({
        "http://localhost:9090/-/ready": {"ready": True},
        "http://localhost:9090/api/v1/query_range*": load_fixture("prometheus/byzantine_range.json"),
    })
    down = FakeHttp({"http://localhost:9090/-/ready": Unreachable("down")})
    result = prometheus_maxima(Prometheus(up, "http://localhost:9090"), 1791172785, 1791172845)
    assert result["available"] is True
    assert result["cometbft_consensus_byzantine_validators"] == {"validator1-node:26660": 0.0, "validator2-node:26660": 0.0}
    assert "cometbft_consensus_missing_validators" in result
    assert all("max_over_time(" in unquote(request["url"]) for request in up.requests if "query_range" in request["url"])
    assert prometheus_maxima(Prometheus(down, "http://localhost:9090"), 0, 1) == {"available": False}


def test_seconds_limit_exceeded_but_block_limit_not():
    """Both ages are required, so exceeding only the time limit is not coverage."""
    scan = scan_evidence(rpc_for(five_blocks()), 100, 104, {"max_age_num_blocks": 100, "max_age_duration_s": 10.0})
    assert scan.span_seconds > 10.0 and scan.span_blocks <= 100
    assert scan.covers_max_age is False


def test_prometheus_empty_result_is_unavailable_and_instances_merge_by_max():
    """No series means the check did not run; two series on one instance keep the larger maximum."""
    def matrix(series):
        """A Prometheus matrix answer with the given (instance, values) series."""
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"instance": instance}, "values": [[1, str(value)] for value in values]} for instance, values in series]}}

    empty = FakeHttp({"http://localhost:9090/-/ready": {"ready": True},
                      "http://localhost:9090/api/v1/query_range*": matrix([])})
    assert prometheus_maxima(Prometheus(empty, "http://localhost:9090"), 0, 60)["available"] is False
    merged = FakeHttp({"http://localhost:9090/-/ready": {"ready": True},
                       "http://localhost:9090/api/v1/query_range*": matrix([("a", [0, 1]), ("a", [0, 0]), ("b", [0])])})
    result = prometheus_maxima(Prometheus(merged, "http://localhost:9090"), 0, 60)
    assert result["cometbft_consensus_byzantine_validators"] == {"a": 1.0, "b": 0.0}
