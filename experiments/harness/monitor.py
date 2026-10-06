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
    """Loaded address per node at one host-clock instant; error is set when the sampling itself failed."""

    t_ms: int
    addresses: dict
    error: str | None = None


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


def indeterminate_seconds(samples: list, validator: str) -> int:
    """Distinct seconds in which a node was unreachable and every other node reported the validator address.

    The unreachable node could have been signing as the validator, so these
    seconds are neither proven single-signer nor proven dual-signer.
    """
    seconds = set()
    for sample in samples:
        reported = sample.addresses.values()
        answered = [address for address in reported if address != UNREACHABLE]
        some_down = len(answered) < len(reported)
        if some_down and answered and all(address == validator for address in answered):
            seconds.add(sample.t_ms // 1000)
    return len(seconds)


def unreachable_seconds(samples: list) -> dict:
    """Distinct seconds per node in which that node did not answer."""
    seconds = {}
    for sample in samples:
        for label, address in sample.addresses.items():
            if address == UNREACHABLE:
                seconds.setdefault(label, set()).add(sample.t_ms // 1000)
    return {label: len(values) for label, values in seconds.items()}


def max_gap_ms(samples: list) -> int:
    """Largest time between consecutive successful samples; 0 with fewer than two."""
    times = [sample.t_ms for sample in samples if sample.error is None]
    return max((later - earlier for earlier, later in zip(times, times[1:])), default=0)


def summarize_monitor(samples: list, validator: str) -> dict:
    """Counts and spans of what the monitor saw: dual, indeterminate, unreachable, gaps, and errors."""
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
        "indeterminate_seconds": indeterminate_seconds(samples, validator),
        "unreachable": unreachable,
        "unreachable_seconds": unreachable_seconds(samples),
        "max_gap_ms": max_gap_ms(samples),
        "errors": sum(1 for sample in samples if sample.error is not None),
    }


COVERAGE_REACH_MS = 5000  # how near an informative sample must be to each window end, and the largest allowed gap


def informative(sample: MonitorSample) -> bool:
    """A sample that shows a loaded key: no sampling error and at least one node answered."""
    return sample.error is None and any(address != UNREACHABLE for address in sample.addresses.values())


def coverage(samples: list, start_ms: int, end_ms: int) -> tuple:
    """(observed, problems): whether the monitor observed the window [start_ms, end_ms].

    The one coverage rule of the harness. A window is observed when an informative sample lies within
    5 s of its start and of its end, no two consecutive informative samples near it are more than 5 s
    apart, and no sampling error happened near it. Each problem is a code with its parameter, so the
    report can word it in Indonesian. Indeterminate seconds are not a coverage problem; window_report
    reports them beside the result.
    """
    reach = COVERAGE_REACH_MS
    nearby = [sample for sample in samples if start_ms - reach <= sample.t_ms <= end_ms + reach]
    times = sorted(sample.t_ms for sample in nearby if informative(sample))
    errors = sum(1 for sample in nearby if sample.error is not None)
    problems = []
    if not times:
        problems.append({"code": "no_samples"})
    else:
        if times[0] > start_ms + reach:
            problems.append({"code": "late_first_sample", "ms": times[0] - start_ms})
        if times[-1] < end_ms - reach:
            problems.append({"code": "early_last_sample", "ms": end_ms - times[-1]})
        largest_gap = max((later - earlier for earlier, later in zip(times, times[1:])), default=0)
        if largest_gap > reach:
            problems.append({"code": "gap", "ms": largest_gap})
    if errors:
        problems.append({"code": "sampling_errors", "count": errors})
    return not problems, problems


def window_report(samples: list, window: dict, validator: str) -> dict:
    """One run window with its coverage, its problems, and the dual-signer and indeterminate seconds inside it."""
    inside = [sample for sample in samples
              if window["start_ms"] <= sample.t_ms <= window["end_ms"] and sample.error is None]
    observed, problems = coverage(samples, window["start_ms"], window["end_ms"])
    return {**window, "observed": observed, "problems": problems,
            "dual_signer_seconds": dual_signer_seconds(inside, validator),
            "indeterminate_seconds": indeterminate_seconds(inside, validator)}


def load_monitor_samples(path) -> list:
    """Read monitor.jsonl back into samples."""
    return [MonitorSample(row["t_ms"], row["addresses"], row.get("error")) for row in read_jsonl(path)]


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
        self.errors = []
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
        """Stop polling, wait for the thread, and return every sample taken; failures are in self.errors."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        return list(self.samples)

    def samples_since(self, since_ms: int) -> list:
        """A copy of the samples taken at or after since_ms, so one iteration can summarize its own window."""
        return [sample for sample in list(self.samples) if sample.t_ms >= since_ms]

    def record_error(self, error: Exception) -> None:
        """Keep a sampling failure as an error sample so the thread never dies silently."""
        message = repr(error)
        self.errors.append(message)
        sample = MonitorSample(self.clock.now_ms(), {}, error=message)
        self.samples.append(sample)
        try:
            append_jsonl(self.path, {"session": self.session_id, **asdict(sample)})
        except OSError as write_error:
            self.errors.append(repr(write_error))

    def _loop(self) -> None:
        """Poll until stopped, recording any failure, keeping one sample per interval when requests are fast."""
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception as error:  # the thread must survive and report, not die
                self.record_error(error)
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self.interval_s - elapsed))
