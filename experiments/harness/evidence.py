"""Forward evidence scan and Prometheus safety series (R7; findings J-4, IV-9, IV-10)."""

from dataclasses import dataclass

from harness.clock import parse_rfc3339_ms

SAFETY_SERIES = ("cometbft_consensus_byzantine_validators", "cometbft_consensus_missing_validators")


@dataclass(frozen=True)
class EvidenceScan:
    """Evidence found in blocks first_height..last_height, and how far the scan reaches into the evidence max age."""

    first_height: int
    last_height: int
    heights_scanned: int
    evidence: list
    first_block_time_ms: int
    last_block_time_ms: int
    span_blocks: int
    span_seconds: float
    max_age_num_blocks: int
    max_age_duration_s: float
    covers_max_age: bool


def scan_evidence(rpc, first_height: int, last_height: int, evidence_params: dict) -> EvidenceScan:
    """Read block.evidence.evidence for every height from the fault height forward to last_height.

    Evidence of an equivocation at height H can only be committed in blocks
    after H, so the scan starts at H and moves forward (IV-10). CometBFT
    expires evidence only when its block age exceeds max_age_num_blocks and
    its time age exceeds max_age_duration (evidence/verify.go
    IsEvidenceExpired); covers_max_age is true when the scan passes both.
    """
    if last_height < first_height:
        raise ValueError(f"scan range {first_height}..{last_height} is empty")
    found = []
    first_time = None
    last_time = None
    for height in range(first_height, last_height + 1):
        block = rpc.block(height)["block"]
        block_time = parse_rfc3339_ms(block["header"]["time"])
        if first_time is None:
            first_time = block_time
        last_time = block_time
        items = block["evidence"]["evidence"] or []
        if items:
            types = sorted({item.get("type", "unknown") for item in items})
            found.append({"height": height, "count": len(items), "types": types})
    span_blocks = last_height - first_height
    span_seconds = (last_time - first_time) / 1000
    max_blocks = evidence_params["max_age_num_blocks"]
    max_seconds = evidence_params["max_age_duration_s"]
    return EvidenceScan(
        first_height=first_height,
        last_height=last_height,
        heights_scanned=last_height - first_height + 1,
        evidence=found,
        first_block_time_ms=first_time,
        last_block_time_ms=last_time,
        span_blocks=span_blocks,
        span_seconds=span_seconds,
        max_age_num_blocks=max_blocks,
        max_age_duration_s=max_seconds,
        covers_max_age=span_blocks > max_blocks and span_seconds > max_seconds,
    )


def prometheus_maxima(prometheus, start_s: int, end_s: int, step_s: int = 15) -> dict:
    """Maximum of each safety series per instance over the window.

    Each step queries max_over_time(metric[step]), so a scrape that fell
    between steps cannot hide a 1. Series on the same instance are merged by
    maximum. {"available": False} when Prometheus is down; also when a series
    returns nothing, because then the check did not run.
    """
    if not prometheus.is_up():
        return {"available": False}
    result = {"available": True, "start_s": start_s, "end_s": end_s, "step_s": step_s}
    for metric in SAFETY_SERIES:
        series = prometheus.query_range(f"max_over_time({metric}[{step_s}s])", start_s, end_s, step_s)
        if not series:
            return {"available": False, "reason": f"no series returned for {metric}"}
        maxima = {}
        for item in series:
            peak = max((value for _, value in item["values"]), default=0.0)
            maxima[item["instance"]] = max(peak, maxima.get(item["instance"], peak))
        result[metric] = maxima
    return result
