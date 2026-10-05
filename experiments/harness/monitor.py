"""Dual-signer monitor (R6): which validator key each node has loaded, sampled at 1 Hz.

`/status` result.validator_info.address names the key the node's process
loaded. If both nodes report the validator address in the same second, two
processes could sign as the same validator; the count of such seconds must be 0.
"""

import threading
import time
from dataclasses import asdict, dataclass

from harness.adapters import Unreachable
from harness.constants import BACKUP_CONTAINER, BACKUP_RPC_URL, PRIMARY_CONTAINER, PRIMARY_RPC_URL
from harness.jsonio import append_jsonl, read_jsonl
from harness.rpc import CometRpc

UNREACHABLE = "unreachable"
MONITOR_TIMEOUT_S = 0.8


@dataclass(frozen=True)
class MonitorSample:
    """Loaded address per node at one host-clock instant."""

    t_ms: int
    addresses: dict


def sample_once(rpcs: dict, clock) -> MonitorSample:
    """Ask every node for its loaded address; a node that does not answer is recorded as unreachable."""
    addresses = {}
    for label, rpc in rpcs.items():
        try:
            addresses[label] = rpc.loaded_address()
        except Unreachable:
            addresses[label] = UNREACHABLE
    return MonitorSample(clock.now_ms(), addresses)


def dual_signer_seconds(samples: list, validator: str) -> int:
    """Distinct wall-clock seconds in which every node reported the validator address."""
    seconds = set()
    for sample in samples:
        reported = list(sample.addresses.values())
        if reported and all(address == validator for address in reported):
            seconds.add(sample.t_ms // 1000)
    return len(seconds)


def summarize_monitor(samples: list, validator: str) -> dict:
    """Sample count, time span, dual-signer seconds, and unreachable samples per node."""
    unreachable = {}
    for sample in samples:
        for label, address in sample.addresses.items():
            if address == UNREACHABLE:
                unreachable[label] = unreachable.get(label, 0) + 1
    return {
        "samples": len(samples),
        "first_ms": samples[0].t_ms if samples else None,
        "last_ms": samples[-1].t_ms if samples else None,
        "dual_signer_seconds": dual_signer_seconds(samples, validator),
        "unreachable": unreachable,
    }


def load_monitor_samples(path) -> list:
    """Read monitor.jsonl back into samples."""
    return [MonitorSample(row["t_ms"], row["addresses"]) for row in read_jsonl(path)]


class DualSignerMonitor:
    """Polls the nodes on a background thread and appends each sample to monitor.jsonl.

    The thread is created in start() and joined in stop(); a runner calls both
    in one try/finally.
    """

    def __init__(self, rpcs: dict, clock, path, session_id: str, interval_s: float = 1.0):
        """Prepare a monitor that has not started."""
        self.rpcs = rpcs
        self.clock = clock
        self.path = path
        self.session_id = session_id
        self.interval_s = interval_s
        self.samples = []
        self._stop = threading.Event()
        self._thread = None

    @classmethod
    def for_session(cls, env, session):
        """Monitor validator1-node and validator2-node with short request timeouts."""
        rpcs = {
            PRIMARY_CONTAINER: CometRpc(env.http, PRIMARY_RPC_URL, timeout=MONITOR_TIMEOUT_S),
            BACKUP_CONTAINER: CometRpc(env.http, BACKUP_RPC_URL, timeout=MONITOR_TIMEOUT_S),
        }
        return cls(rpcs, env.clock, session.directory / "monitor.jsonl", session.id)

    def poll_once(self) -> MonitorSample:
        """Take one sample, keep it, and append it to the file."""
        sample = sample_once(self.rpcs, self.clock)
        self.samples.append(sample)
        append_jsonl(self.path, {"session": self.session_id, **asdict(sample)})
        return sample

    def start(self) -> None:
        """Begin polling on a daemon thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> list:
        """Stop polling, wait for the thread, and return every sample taken."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        return list(self.samples)

    def _loop(self) -> None:
        """Poll until stopped, keeping one sample per interval when the requests are fast."""
        while not self._stop.is_set():
            started = time.monotonic()
            self.poll_once()
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self.interval_s - elapsed))
