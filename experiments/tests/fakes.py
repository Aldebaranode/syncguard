"""Test doubles for the harness adapters and shared fixture helpers."""

import json
from pathlib import Path

from harness.adapters import CommandResult, RecordedHttp
from harness.environment import Paths

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Read a JSON fixture by its path below tests/fixtures."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeHttp(RecordedHttp):
    """RecordedHttp that logs every request; a list answer is served in order and its last item repeats."""

    def __init__(self, answers: dict | None = None):
        """Start with the given answers and an empty request log."""
        super().__init__(answers or {})
        self.requests = []

    def request(self, method, url, headers=None, body=None, timeout=5.0):
        """Log the request, then answer it like RecordedHttp."""
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {}), "body": body})
        return super().request(method, url, headers, body, timeout)

    def lookup(self, url):
        """Resolve the answer and step through list answers."""
        answer = super().lookup(url)
        if isinstance(answer, list):
            if len(answer) == 1:
                return answer[0]
            return answer.pop(0)
        return answer


class FakeCommands:
    """Scripted command results keyed by the argv tuple; every call is recorded."""

    def __init__(self, script: dict | None = None, clock=None):
        """script maps an argv tuple to a list of (exit_code, stdout, stderr); the last one repeats."""
        self.script = {tuple(key): list(value) for key, value in (script or {}).items()}
        self.calls = []
        self.spawned = []
        self.clock = clock

    def run(self, argv, timeout=120.0):
        """Return the next scripted result for argv; an unscripted command fails the test."""
        key = tuple(argv)
        self.calls.append(list(argv))
        if key not in self.script:
            raise AssertionError(f"unexpected command: {argv}")
        answers = self.script[key]
        exit_code, stdout, stderr = answers[0] if len(answers) == 1 else answers.pop(0)
        stamp = self.clock.now_ms() if self.clock is not None else 0
        return CommandResult(list(argv), exit_code, stdout, stderr, stamp, stamp)

    def spawn(self, argv):
        """Record a background start and return a handle that records its stop."""
        process = FakeProcess(list(argv))
        self.spawned.append(process)
        return process


class FakeProcess:
    """Handle of a fake background process."""

    def __init__(self, argv):
        """Remember the argv; not stopped yet."""
        self.argv = argv
        self.stopped = False

    def stop(self):
        """Mark the process stopped."""
        self.stopped = True


class FakeClock:
    """Deterministic clock: sleep advances time instantly and is recorded."""

    def __init__(self, start_ms: int):
        """Start at start_ms."""
        self.now = start_ms
        self.slept = []

    def now_ms(self) -> int:
        """Current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance the fake time by seconds."""
        self.slept.append(seconds)
        self.now += round(seconds * 1000)


def paths_under(root: Path) -> Paths:
    """The evaluation file layout below a temporary root, for tests."""
    story = root / "story"
    return Paths(
        data_dir=root / "data",
        syncguard_dir=root / "syncguard",
        localnet_dir=root / "localnet",
        genesis_file=root / "localnet" / "genesis-node.json",
        active_config=root / "syncguard" / "config.yaml",
        passive_config=root / "syncguard" / "config-passive.yaml",
        active_log=root / "syncguard" / "syncguard.log",
        passive_log=root / "syncguard" / "syncguard-passive.log",
        primary_key=story / "validator1" / "priv_validator_key.json",
        primary_state=story / "validator1" / "data" / "priv_validator_state.json",
        backup_key=story / "validator2" / "config" / "priv_validator_key.json",
        backup_state=story / "validator2" / "data" / "priv_validator_state.json",
        runbook=root / "runbooks" / "manual-recovery.md",
    )
