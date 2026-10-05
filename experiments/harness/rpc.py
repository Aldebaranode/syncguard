"""Typed, read-only access to CometBFT RPC, the Story API, and Prometheus."""

import base64
from urllib.parse import urlencode

from harness.adapters import Unreachable


class CometRpc:
    """Read-only CometBFT RPC client for one node."""

    def __init__(self, http, base_url: str, timeout: float = 5.0):
        """Bind the client to one node's RPC base URL."""
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _result(self, path: str) -> dict:
        """GET path and return the JSON-RPC result object."""
        body = self.http.get_json(self.base_url + path, timeout=self.timeout)
        if "result" not in body:
            raise Unreachable(f"{self.base_url}{path}: answer has no result")
        return body["result"]

    def status(self) -> dict:
        """The /status result."""
        return self._result("/status")

    def tip_height(self) -> int:
        """Latest block height the node knows."""
        return int(self.status()["sync_info"]["latest_block_height"])

    def loaded_address(self) -> str:
        """Address of the validator key the node's process loaded."""
        return self.status()["validator_info"]["address"]

    def commit(self, height: int) -> dict:
        """The /commit result for one height."""
        return self._result(f"/commit?height={height}")

    def block(self, height: int) -> dict:
        """The /block result for one height."""
        return self._result(f"/block?height={height}")

    def validators(self) -> list:
        """The validator set as address, voting power, and base64 public key."""
        result = self._result("/validators")
        return [
            {"address": item["address"], "voting_power": int(item["voting_power"]), "pub_key": item["pub_key"]["value"]}
            for item in result["validators"]
        ]

    def evidence_params(self) -> dict:
        """Evidence max age in blocks and in seconds."""
        evidence = self._result("/consensus_params")["consensus_params"]["evidence"]
        return {
            "max_age_num_blocks": int(evidence["max_age_num_blocks"]),
            "max_age_duration_s": int(evidence["max_age_duration"]) / 1e9,
        }


def pubkey_hex_from_base64(pubkey_b64: str) -> str:
    """Convert a base64 compressed secp256k1 key to the 0x-hex form that /slashing/signing_infos/{pubkey} expects."""
    return "0x" + base64.b64decode(pubkey_b64).hex()


class StoryApi:
    """Read-only client for the Story node API, which wraps answers as {code, msg, error}."""

    def __init__(self, http, base_url: str):
        """Bind the client to the API base URL."""
        self.http = http
        self.base_url = base_url.rstrip("/")

    def _msg(self, path: str) -> dict:
        """GET path and return msg; a code other than 200 raises Unreachable."""
        body = self.http.get_json(self.base_url + path)
        if body.get("code") != 200:
            raise Unreachable(f"{self.base_url}{path}: code {body.get('code')} {body.get('error')}")
        return body["msg"]

    def slashing_params(self) -> dict:
        """Slashing parameters as numbers (durations in seconds)."""
        params = self._msg("/slashing/params")["params"]
        return {
            "signed_blocks_window": int(params["signed_blocks_window"]),
            "min_signed_per_window": float(params["min_signed_per_window"]),
            "downtime_jail_duration_s": int(params["downtime_jail_duration"]) / 1e9,
            "slash_fraction_double_sign": float(params["slash_fraction_double_sign"]),
            "slash_fraction_downtime": float(params["slash_fraction_downtime"]),
        }

    def signing_info(self, pubkey_b64: str) -> dict:
        """Signing info of one validator; fields omitted by amino JSON take their zero value."""
        path = "/slashing/signing_infos/" + pubkey_hex_from_base64(pubkey_b64)
        info = self._msg(path)["val_signing_info"]
        return {
            "url_path": path,
            "cons_address": info["address"],
            "missed_blocks_counter": int(info.get("missed_blocks_counter", 0)),
            "jailed_until": info.get("jailed_until", ""),
            "tombstoned": bool(info.get("tombstoned", False)),
            "index_offset": int(info.get("index_offset", 0)),
            "start_height": int(info.get("start_height", 0)),
        }


class Prometheus:
    """Read-only Prometheus HTTP API client."""

    def __init__(self, http, base_url: str):
        """Bind the client to the Prometheus base URL."""
        self.http = http
        self.base_url = base_url.rstrip("/")

    def is_up(self) -> bool:
        """Whether the readiness endpoint answers 200."""
        try:
            return self.http.request("GET", self.base_url + "/-/ready").status == 200
        except Unreachable:
            return False

    def query_range(self, query: str, start_s: int, end_s: int, step_s: int) -> list:
        """Each series of a range query as its instance label and (unix seconds, value) pairs."""
        params = urlencode({"query": query, "start": start_s, "end": end_s, "step": step_s})
        body = self.http.get_json(f"{self.base_url}/api/v1/query_range?{params}")
        if body.get("status") != "success":
            raise Unreachable(f"Prometheus query failed: {body.get('error')}")
        return [
            {"instance": item["metric"].get("instance", ""), "values": [(int(t), float(v)) for t, v in item["values"]]}
            for item in body["data"]["result"]
        ]
