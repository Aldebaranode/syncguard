"""Test doubles for the harness adapters and shared fixture helpers."""

import itertools
import json
import shutil
from pathlib import Path

from harness.adapters import CommandResult, RecordedHttp, Unreachable
from harness.clock import format_local, format_utc
from harness.environment import Paths, build_environment
from harness.jsonio import append_jsonl, read_json, write_json
from harness.report import SessionData
from harness.session import open_session
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
    """Scripted command results keyed by the argv tuple; every call and its timeout is recorded."""

    def __init__(self, script: dict | None = None, clock=None, durations: dict | None = None):
        """script maps an argv tuple to a list of (exit_code, stdout, stderr); the last one repeats.

        An exception in the list (such as KeyboardInterrupt()) is raised by that call, as a Ctrl-C while it runs.
        durations maps an argv tuple to how many ms that command takes, so its ended time differs from its start.
        timeouts[i] and cwds[i] are the timeout and working directory the caller gave calls[i].
        """
        self.script = {tuple(key): list(value) for key, value in (script or {}).items()}
        self.durations = {tuple(key): value for key, value in (durations or {}).items()}
        self.calls = []
        self.timeouts = []
        self.cwds = []
        self.spawned = []
        self.clock = clock

    def run(self, argv, timeout=120.0, cwd=None):
        """Return the next scripted result for argv; an unscripted command fails the test."""
        key = tuple(argv)
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        self.cwds.append(cwd)
        if key not in self.script:
            raise AssertionError(f"unexpected command: {argv}")
        answers = self.script[key]
        answer = answers[0] if len(answers) == 1 else answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        exit_code, stdout, stderr = answer
        stamp = self.clock.now_ms() if self.clock is not None else 0
        return CommandResult(list(argv), exit_code, stdout, stderr, stamp, stamp + self.durations.get(key, 0))

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


BINARY_REVISION = "11bbc8f80343cc03bc6219509055ed552f55f883"


def go_version_output(modified: str = "false") -> str:
    """The relevant lines of `go version -m bin/syncguard`; modified is the vcs.modified value."""
    return ("bin/syncguard: go1.25.6\n\tpath\tgithub.com/aldebaranode/syncguard/cli\n"
            "\tdep\tgithub.com/spf13/cobra\tv1.9.1\th1:abc\n\tbuild\tGOARCH=arm64\n\tbuild\tvcs=git\n"
            f"\tbuild\tvcs.revision={BINARY_REVISION}\n\tbuild\tvcs.time=2026-10-05T04:15:24Z\n"
            f"\tbuild\tvcs.modified={modified}\n")


def metadata_script(syncguard_dir: Path, localnet_dir: Path) -> dict:
    """FakeCommands answers for every command collect_metadata runs (values seen on 2026-10-05)."""
    adjtimex = (FIXTURES / "adjtimex.txt").read_text(encoding="utf-8")
    return {
        ("git", "-C", str(syncguard_dir), "describe", "--always", "--dirty"): [(0, "thesis-eval-1-6-g03d1187-dirty\n", "")],
        ("git", "-C", str(syncguard_dir), "rev-parse", "HEAD"): [(0, "03d1187aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", "")],
        ("git", "-C", str(syncguard_dir), "describe", "--always", BINARY_REVISION): [(0, "thesis-eval-2\n", "")],
        ("go", "version", "-m", str(syncguard_dir / "bin" / "syncguard")): [(0, go_version_output(), "")],
        ("git", "-C", str(localnet_dir), "rev-parse", "HEAD"): [(0, "f9e4fc4bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n", "")],
        ("git", "-C", str(localnet_dir), "rev-parse", "--abbrev-ref", "HEAD"): [(0, "thesis-four-validators\n", "")],
        ("docker", "image", "inspect", "--format", "{{.Id}}", "story-node:localnet"): [(0, "sha256:01ad91f72b07\n", "")],
        ("docker", "image", "inspect", "--format", "{{.Id}}", "story-geth:localnet"): [(0, "sha256:8adf069c592f\n", "")],
        ("docker", "image", "inspect", "--format", "{{.Id}}", "prom/prometheus:latest"): [(0, "sha256:aa11bb22cc33\n", "")],
        ("docker", "image", "inspect", "--format", "{{.Id}}", "grafana/grafana:latest"): [(0, "sha256:dd44ee55ff66\n", "")],
        ("docker", "image", "inspect", "--format", "{{.Id}}", "grafana/loki:latest"): [(0, "sha256:1234567890ab\n", "")],
        ("docker", "image", "inspect", "--format", "{{.Id}}", "grafana/promtail:latest"): [(1, "", "Error: No such image\n")],
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
    binary = paths.syncguard_dir / "bin" / "syncguard"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"fake-binary")


class FakeMonitor:
    """Stand-in for DualSignerMonitor that records start and stop."""

    def __init__(self):
        """Neither started nor stopped yet."""
        self.started = 0
        self.stopped = 0

    def start(self):
        """Count a start."""
        self.started += 1

    def stop(self):
        """Count a stop and return no samples."""
        self.stopped += 1
        return []

    def samples_since(self, since_ms):
        """No samples."""
        return []


def node_status_answers(primary_fixture: str, backup_fixture: str) -> dict:
    """FakeHttp answers for the /status of validator1-node and validator2-node."""
    return {
        "http://localhost:21657/status": load_fixture(primary_fixture),
        "http://localhost:22657/status": load_fixture(backup_fixture),
    }


def make_session(data_dir: Path, clock, session_id: str = "S4", offset_ms: float = 0.0, extra: dict | None = None):
    """A started session directory with minimal metadata, opened as a runner opens it."""
    metadata = {
        "session": session_id,
        "purpose": "test",
        "start_ms": clock.now_ms(),
        "start_local": format_local(clock.now_ms(), 25200),
        "host_utc_offset_seconds": 25200,
        "config_id": "cfg",
        "syncguard_version": "test-build",
        "clock_offset": {"offset_ms": offset_ms},
        "slashing_params": {"signed_blocks_window": 200, "min_signed_per_window": 0.05},
        **(extra or {}),
    }
    write_json(data_dir / session_id / "metadata.json", metadata)
    return open_session(data_dir, session_id, clock)


def block_answers(times: dict, evidence: dict | None = None):
    """A FakeHttp answer serving /block?height=N from {N: RFC 3339 time} and optional {N: [evidence items]}."""
    evidence = evidence or {}

    def answer(url: str):
        """The synthetic block for the height in url."""
        height = int(url.rsplit("=", 1)[1])
        if height not in times:
            return Unreachable(f"no block for height {height}")
        block = {"header": {"height": str(height), "time": times[height]}, "evidence": {"evidence": evidence.get(height, [])}}
        return {"jsonrpc": "2.0", "id": -1, "result": {"block": block}}

    return answer


NOT_A_KEY = "TEST-ONLY-NOT-A-KEY"


def write_key(path: Path, address: str) -> None:
    """A key file shaped like priv_validator_key.json with a dummy private value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "address": address,
        "pub_key": {"type": "tendermint/PubKeySecp256k1", "value": "AvLo"},
        "priv_key": {"type": "tendermint/PrivKeySecp256k1", "value": NOT_A_KEY},
    }), encoding="utf-8")


def write_state(path: Path, height: int) -> None:
    """A priv_validator_state.json with height as a string, as CometBFT writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"height": str(height), "round": 0, "step": 3, "signature": "c2ln", "signbytes": "AA"}),
                    encoding="utf-8")


READY_NOW = 1791169200000  # 2026-10-05T03:00:00Z: 238 s after the fixture's last failback (02:56:01.698Z)
# The two commands take_snapshot runs in each node's state directory, answered alike for both nodes.
SNAPSHOT_COMMANDS = {
    ("cat", "priv_validator_state.json"): [
        (0, '{"height": "180", "round": 0, "step": 3, "signature": "c2ln", "signbytes": "AA"}', "")],
    ("ls", "-l", "priv_validator_state.json.lock"): [
        (0, "-rw-------  1 heed  staff  5 Oct  5 10:00 priv_validator_state.json.lock\n", "")],
}
SECRET = "test-only-shared-secret-value"
TARGET_ADDRESS = "48DC218393FCEEF56A37D963B804FAB92C62CA9D"
MOCK_ADDRESS = "E6FAA3F2B3E9158BA9BF93BE1F7F8D3095EAD04D"
PS_OUTPUT = (
    "  4242 ./bin/syncguard --config config.yaml --role active\n"
    "  4243 ./bin/syncguard --config config-passive.yaml --role passive\n"
    "  777 /Users/heed/Projects/syncguard/experiments/.venv/bin/python -m harness run failover --session S4\n"
)


def rising_status(start: int, step: int = 1):
    """A /status answer whose latest height rises by step on every call."""
    heights = itertools.count(start, step)

    def answer(url: str):
        """The next height."""
        return {"jsonrpc": "2.0", "id": -1, "result": {"sync_info": {"latest_block_height": str(next(heights))}}}

    return answer


def signed_commit_answer(signers: list):
    """A /commit answer for any height, signed by every address in signers, 3 s per height."""

    def answer(url: str):
        """A synthetic commit for the height in url."""
        height = int(url.rsplit("=", 1)[1])
        stamp = format_utc(1791169000000 + height * 3000)
        return {"jsonrpc": "2.0", "id": -1, "result": make_commit(height, stamp, {address: stamp for address in signers})}

    return answer


def ready_env(tmp_path: Path, ps_output: str = PS_OUTPUT, extra_script: dict | None = None,
              extra_http: dict | None = None, clock=None, durations: dict | None = None):
    """An environment in which every failover precondition holds; extra answers override or extend it."""
    paths = paths_under(tmp_path)
    write_configs(paths, SECRET)
    write_key(paths.primary_key, TARGET_ADDRESS)
    write_key(paths.backup_key, MOCK_ADDRESS)
    write_state(paths.primary_state, 180)
    write_state(paths.backup_state, 175)
    Path(f"{paths.primary_state}.lock").write_text("4242\n", encoding="utf-8")
    shutil.copy(FIXTURES / "logs" / "smoke-active.txt", paths.active_log)
    shutil.copy(FIXTURES / "logs" / "smoke-passive.txt", paths.passive_log)
    paths.runbook.parent.mkdir(parents=True, exist_ok=True)
    paths.runbook.write_text("# Manual recovery\n", encoding="utf-8")
    clock = clock or FakeClock(READY_NOW)
    script = {
        ("ps", "-axo", "pid=,command="): [(0, ps_output, "")],
        ("ps", "-p", "4242", "-o", "pid="): [(0, " 4242\n", "")],
        ("ps", "-p", "4243", "-o", "pid="): [(0, " 4243\n", "")],
        **SNAPSHOT_COMMANDS,
        **(extra_script or {}),
    }
    answers = {
        **node_status_answers("rpc/status_validator1.json", "rpc/status_validator2.json"),
        "http://localhost:26657/status": rising_status(1188),
        "http://localhost:26657/commit?height=*": signed_commit_answer([TARGET_ADDRESS, MOCK_ADDRESS]),
        "http://localhost:8081/health": {"healthy": True, "active": False},
        **(extra_http or {}),
    }
    return build_environment(FakeHttp(answers), FakeCommands(script, clock, durations), clock, paths, 25200)


class FakeProbes:
    """Scripted LiveProbes: markers by (node, marker), signatures and misses served in call order."""

    def __init__(self, markers: dict, signatures: list, misses: list | None = None):
        """Keep the scripted answers and an empty call log."""
        self.markers = markers
        self.signatures = list(signatures)
        self.misses = list(misses or [])
        self.calls = []

    def marker(self, node, marker, since_ms, timeout_s):
        """The scripted event for (node, marker), or None."""
        self.calls.append(("marker", node, marker))
        return self.markers.get((node, marker))

    def signature(self, after_ms, start_height, timeout_s):
        """The next scripted signature, or None."""
        self.calls.append(("signature", after_ms))
        return self.signatures.pop(0) if self.signatures else None

    def miss(self, start_height, timeout_s):
        """The next scripted missed height, or None."""
        self.calls.append(("miss", start_height))
        return self.misses.pop(0) if self.misses else None


TENDERDUTY_T0 = 1791169200000  # 2026-10-05 10:00:00 WIB, the header time of height 100
S10_HEADER_14858_MS = 1791553033043  # Sesi S10's stored header time of 14858, the tip at its fault (containers' clock)


def tenderduty_height_line(session_id: str, height: int, vote: str = "signed", proposer: str = MOCK_ADDRESS,
                           anchor: tuple = (100, TENDERDUTY_T0)) -> dict:
    """One stored commit of the Tenderduty evidence, 3 s per height from anchor, a (height, header time) pair;
    48DC2183's vote is signed, nil, or absent."""
    anchor_height, anchor_ms = anchor
    stamp = anchor_ms + (height - anchor_height) * 3000
    return {"session": session_id, "height": height, "header_time_ms": stamp, "proposer": proposer,
            "commit_round": 0, "signers": {TARGET_ADDRESS: stamp} if vote == "signed" else {},
            "nil_voters": {TARGET_ADDRESS: stamp} if vote == "nil" else {}, "absent": 1 if vote == "absent" else 0}


def write_incident_heights(directory: Path, session_id: str, first: int, last: int, votes: dict,
                           proposers: dict | None = None, anchor: tuple = (100, TENDERDUTY_T0)) -> None:
    """heights.jsonl of first to last: 48DC2183 signed unless votes says nil or absent, each block proposed by
    MOCK_ADDRESS unless proposers names its proposer, and 3 s per height from anchor (tenderduty_height_line)."""
    for height in range(first, last + 1):
        proposer = (proposers or {}).get(height, MOCK_ADDRESS)
        append_jsonl(directory / "heights.jsonl",
                     tenderduty_height_line(session_id, height, votes.get(height, "signed"), proposer, anchor))


# 48DC2183's votes around Sesi S10's failover: absent at 14859 to 14865, until the backup's first signature at 14866,
# and at 14887 to 14889, until the primary's first signature after failback at 14890, as on the chain; the nil vote
# at 14875 is added for the tests.
INCIDENT_VOTES = {**dict.fromkeys(range(14859, 14866), "absent"), 14875: "nil",
                  **dict.fromkeys(range(14887, 14890), "absent")}


def write_incident_evidence(directory: Path, session_id: str, votes: dict = INCIDENT_VOTES,
                            proposers: dict | None = None, last_height: int = 14895) -> None:
    """S10's incident: the fault of iteration 1 at height 14858 in events.jsonl and heights 14850 to last_height, 3 s
    apart around S10's header time of 14858, so the subscription of Tenderduty's log that spans them also ran then."""
    append_jsonl(directory / "events.jsonl", {"session": session_id, "t_ms": 1791553038331, "kind": "fault_injected",
                                              "scenario": "failover", "iteration": 1, "height": 14858, "exit_code": 0,
                                              "ended_ms": 1791553038631})
    write_incident_heights(directory, session_id, 14850, last_height, votes, proposers,
                           anchor=(14858, S10_HEADER_14858_MS))


def write_tenderduty_evidence(directory: Path, session_id: str, start_tip: int | None = 101, end_tip: int = 109,
                              missed: tuple = (2, 5)) -> None:
    """Heights 100 to 109 in heights.jsonl and a start and an end Tenderduty reading, at 10:00:01 and 10:00:25.

    The readings were taken at tips 101 and 109, so their change covers the commits of 101 to 108. 48DC2183
    has no commit signature at 103 to 105 (absent) and 107 (a nil vote), so three heights of that range are
    absent; Tenderduty's missed_blocks rises from missed[0] to missed[1]. start_tip None writes the start the
    way an operator writes it by hand: t_ms and the count, without a tip.
    """
    votes = {103: "absent", 104: "absent", 105: "absent", 107: "nil"}
    for height in range(100, 110):
        append_jsonl(directory / "heights.jsonl", tenderduty_height_line(session_id, height, votes.get(height, "signed")))
    start = {"session": session_id, "point": "start", "t_ms": TENDERDUTY_T0 + 1000, "missed_blocks": missed[0]}
    if start_tip is None:
        start["source"] = "ditulis tangan"
    else:
        start.update(signed_blocks=10, proposed_blocks=1, missed_blocks_for_window=missed[0], tip_height=start_tip)
    end = {"session": session_id, "point": "end", "t_ms": TENDERDUTY_T0 + 25_000, "missed_blocks": missed[1],
           "signed_blocks": 10, "proposed_blocks": 1, "missed_blocks_for_window": missed[1], "tip_height": end_tip}
    for reading in (start, end):
        append_jsonl(directory / "tenderduty.jsonl", reading)


def make_data(optional: dict | None = None, analysis: dict | None = None, session: str = "S9",
              directory: Path | None = None, snapshots: list | None = None) -> SessionData:
    """An in-memory SessionData with minimal metadata, for report builders that read only these dictionaries."""
    metadata = {"session": session, "host_utc_offset_seconds": 25200, "start_ms": 0, "start_local": "2026-10-05 09:00:00.000",
                "syncguard_version": "test-build", "clock_offset": {"offset_ms": 0.0}}
    base = {"session": session, "failover": [], "failback": [], "incidents": [], "hang": [], "soak": None}
    return SessionData(directory or Path("."), metadata, {**base, **(analysis or {})}, optional or {}, snapshots or [])


S5_SERIES = FIXTURES / "analysis" / "s5-series.json"  # failover, failback, series_aborted of data/S5/analysis.json


def s5_series(**analysis) -> SessionData:
    """Sesi S5 with its series rows, copied from data/S5/analysis.json; analysis replaces some of them."""
    return make_data(analysis={**read_json(S5_SERIES), **analysis}, session="S5")
