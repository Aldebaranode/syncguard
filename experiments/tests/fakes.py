"""Test doubles for the harness adapters and shared fixture helpers."""

import json
from pathlib import Path

from harness.adapters import CommandResult, RecordedHttp, Unreachable
from harness.environment import Paths
from harness.signing import parse_commit

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


def commit_answers(commits: dict):
    """A FakeHttp answer serving /commit?height=N from a {"N": result} mapping."""

    def answer(url: str):
        """The recorded commit for the height in url, or Unreachable."""
        height = url.rsplit("=", 1)[1]
        if height not in commits:
            return Unreachable(f"no commit fixture for height {height}")
        return {"jsonrpc": "2.0", "id": -1, "result": commits[height]}

    return answer


def smoke_records() -> list:
    """HeightRecords of the recorded smoke-test commits (container clock)."""
    return [parse_commit(result) for result in load_fixture("rpc/commits_smoke.json").values()]


def make_commit(height: int, time_iso: str, signers: dict, nil_voters: dict | None = None, absent: int = 0) -> dict:
    """A synthetic /commit result; signers and nil_voters map address to an RFC 3339 timestamp."""
    signatures = [{"block_id_flag": 2, "validator_address": address, "timestamp": stamp}
                  for address, stamp in signers.items()]
    signatures += [{"block_id_flag": 3, "validator_address": address, "timestamp": stamp}
                   for address, stamp in (nil_voters or {}).items()]
    signatures += [{"block_id_flag": 1, "validator_address": "", "timestamp": "0001-01-01T00:00:00Z"}] * absent
    return {
        "signed_header": {
            "header": {"height": str(height), "time": time_iso, "proposer_address": "7EFAECCD2CC260C216ECAFB28C245329B91F3CEF"},
            "commit": {"height": str(height), "round": 0, "signatures": signatures},
        },
        "canonical": True,
    }


ADJTIMEX_ARGV = ("docker", "exec", "validator3-node", "busybox", "adjtimex")


def metadata_script(syncguard_dir: Path, localnet_dir: Path) -> dict:
    """FakeCommands answers for every command collect_metadata runs (values seen on 2026-10-05)."""
    adjtimex = (FIXTURES / "adjtimex.txt").read_text(encoding="utf-8")
    return {
        ("git", "-C", str(syncguard_dir), "describe", "--always", "--dirty"): [(0, "thesis-eval-1-6-g03d1187-dirty\n", "")],
        ("git", "-C", str(syncguard_dir), "rev-parse", "HEAD"): [(0, "03d1187aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", "")],
        ("git", "-C", str(localnet_dir), "rev-parse", "HEAD"): [(0, "f9e4fc4bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n", "")],
        ("git", "-C", str(localnet_dir), "rev-parse", "--abbrev-ref", "HEAD"): [(0, "thesis-four-validators\n", "")],
        ("docker", "image", "inspect", "--format", "{{.Id}}", "story-node:localnet"): [(0, "sha256:01ad91f72b07\n", "")],
        ("docker", "image", "inspect", "--format", "{{.Id}}", "story-geth:localnet"): [(0, "sha256:8adf069c592f\n", "")],
        ADJTIMEX_ARGV: [(0, adjtimex, "")],
        ("sysctl", "-n", "machdep.cpu.brand_string"): [(0, "Apple M4\n", "")],
        ("sysctl", "-n", "hw.memsize"): [(0, "25769803776\n", "")],
        ("sw_vers", "-productVersion"): [(0, "15.6.1\n", "")],
        ("docker", "version", "--format", "{{.Server.Version}} {{.Server.Arch}}"): [(0, "28.3.2 arm64\n", "")],
    }


def metadata_http() -> FakeHttp:
    """FakeHttp answering the RPC and API reads of collect_metadata."""
    return FakeHttp({
        "http://localhost:26657/validators": load_fixture("rpc/validators.json"),
        "http://localhost:26657/consensus_params": load_fixture("rpc/consensus_params.json"),
        "http://localhost:1317/slashing/params": load_fixture("story/slashing_params.json"),
    })


def write_configs(paths, secret: str) -> None:
    """Write minimal active and passive configs and a genesis file below a test root."""
    paths.active_config.parent.mkdir(parents=True, exist_ok=True)
    paths.active_config.write_text(f'secret: "{secret}"\nnode:\n  id: "validator-1"\n  port: 8090\n', encoding="utf-8")
    paths.passive_config.write_text(f'secret: "{secret}"\nnode:\n  id: "validator-2"\n  port: 8081\n', encoding="utf-8")
    paths.genesis_file.parent.mkdir(parents=True, exist_ok=True)
    paths.genesis_file.write_text("{}", encoding="utf-8")
