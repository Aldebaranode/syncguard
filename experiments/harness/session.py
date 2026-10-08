"""Session identity (R2): ids, metadata.json, redacted configs, and the append-only events.jsonl."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from harness.buildinfo import parse_build_info, require_clean_build
from harness.clock import format_local, format_utc
from harness.clockoffset import measure_clock_offset
from harness.configs import redact_config
from harness.constants import CLOCK_CONTAINER, IMAGES
from harness.jsonio import append_jsonl, read_jsonl, write_json

SESSION_ID = re.compile(r"^S(\d+)$")
LAST_ARCHIVED_SESSION = 3


def next_session_id(data_dir: Path) -> str:
    """The next free S<n>; S1-S3 are the archived sessions, so the first new id is S4."""
    highest = LAST_ARCHIVED_SESSION
    if data_dir.exists():
        for child in data_dir.iterdir():
            match = SESSION_ID.match(child.name)
            if child.is_dir() and match:
                highest = max(highest, int(match.group(1)))
    return f"S{highest + 1}"


class EventLog:
    """Append-only events.jsonl of one session; every row carries the session id and a host-clock time."""

    def __init__(self, path: Path, session_id: str, clock):
        """Bind the log to its file, session, and clock."""
        self.path = path
        self.session_id = session_id
        self.clock = clock

    def append(self, kind: str, t_ms: int | None = None, **fields) -> dict:
        """Write one event; t_ms defaults to now on the host clock."""
        stamp = self.clock.now_ms() if t_ms is None else t_ms
        event = {"session": self.session_id, "t_ms": stamp, "t_utc": format_utc(stamp), "kind": kind, **fields}
        append_jsonl(self.path, event)
        return event


def read_events(path: Path) -> list:
    """All events of a session in file order."""
    return read_jsonl(path)


def sha256_file(path: Path) -> str:
    """sha256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_checked(commands, argv: list) -> str:
    """Run a command whose output is mandatory metadata; a failure stops the session start."""
    result = commands.run(argv)
    if result.exit_code != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def host_description(commands) -> str:
    """CPU, RAM, macOS version, and Docker server version in one line."""
    cpu = _run_checked(commands, ["sysctl", "-n", "machdep.cpu.brand_string"])
    memory_gb = int(_run_checked(commands, ["sysctl", "-n", "hw.memsize"])) // 2**30
    macos = _run_checked(commands, ["sw_vers", "-productVersion"])
    docker = _run_checked(commands, ["docker", "version", "--format", "{{.Server.Version}} {{.Server.Arch}}"])
    return f"{cpu}, {memory_gb} GB RAM, macOS {macos}, Docker {docker}"


def config_id(redacted: dict, build: dict) -> str:
    """Short hash over the binary, the environment it ran in, and both redacted configs.

    build holds binary_sha256, vcs_revision, localnet_commit, images, and genesis_sha256; the harness
    checkout does not enter, so iterations whose ids differ are never pooled and the same binary keeps
    its id. The harness checkout is stored beside it as harness_describe.
    """
    digest = hashlib.sha256(json.dumps(build, sort_keys=True).encode("utf-8"))
    for name in sorted(redacted):
        digest.update(name.encode("utf-8"))
        digest.update(redacted[name].encode("utf-8"))
    return digest.hexdigest()[:12]


def collect_metadata(env, session_id: str, purpose: str) -> tuple:
    """Gather every R2 fact; return the metadata and the redacted config texts to store beside it."""
    started = env.clock.now_ms()
    redacted = {}
    fingerprints = {}
    for name, path in (("config", env.paths.active_config), ("config-passive", env.paths.passive_config)):
        text, fingerprint = redact_config(path.read_text(encoding="utf-8"))
        if not fingerprint:
            raise ValueError(f"{path.name} has no secret line; syncguard configs always carry one")
        redacted[name] = text
        fingerprints[name] = fingerprint
    git_syncguard = ["git", "-C", str(env.paths.syncguard_dir)]
    git_localnet = ["git", "-C", str(env.paths.localnet_dir)]
    binary = env.paths.syncguard_dir / "bin" / "syncguard"
    binary_build = parse_build_info(_run_checked(env.commands, ["go", "version", "-m", str(binary)]))
    require_clean_build(binary_build)
    harness_describe = _run_checked(env.commands, git_syncguard + ["describe", "--always", "--dirty"])
    build = {
        "binary_sha256": sha256_file(binary),
        "vcs_revision": binary_build.vcs_revision,
        "localnet_commit": _run_checked(env.commands, git_localnet + ["rev-parse", "HEAD"]),
        "images": {
            image: _run_checked(env.commands, ["docker", "image", "inspect", "--format", "{{.Id}}", image])
            for image in IMAGES
        },
        "genesis_sha256": sha256_file(env.paths.genesis_file),
    }
    metadata = {
        "session": session_id,
        "purpose": purpose,
        "start_ms": started,
        "start_utc": format_utc(started),
        "start_local": format_local(started, env.utc_offset_seconds),
        "host_utc_offset_seconds": env.utc_offset_seconds,
        "harness_describe": harness_describe,
        "harness_commit": _run_checked(env.commands, git_syncguard + ["rev-parse", "HEAD"]),
        "syncguard_build": asdict(binary_build),
        "syncguard_commit": binary_build.vcs_revision,
        "syncguard_version": _run_checked(env.commands, git_syncguard + ["describe", "--always", binary_build.vcs_revision]),
        "syncguard_binary_sha256": build["binary_sha256"],
        "localnet_commit": build["localnet_commit"],
        "localnet_branch": _run_checked(env.commands, git_localnet + ["rev-parse", "--abbrev-ref", "HEAD"]),
        "images": build["images"],
        "genesis_sha256": build["genesis_sha256"],
        "validators": env.rpc.validators(),
        "slashing_params": env.story.slashing_params(),
        "evidence_params": env.rpc.evidence_params(),
        "secret_fingerprints": fingerprints,
        "config_id": config_id(redacted, build),
        "clock_offset": asdict(measure_clock_offset(env.commands, CLOCK_CONTAINER)),
        "host": host_description(env.commands),
    }
    return metadata, redacted


def start_session(env, purpose: str) -> str:
    """Create the next session: collect metadata first, then write metadata.json, configs, and session_start.

    collect_metadata redacts the configs and verifies the secret is gone (redact_config raises
    otherwise), so a refused config stops the start before any file is written.
    """
    session_id = next_session_id(env.paths.data_dir)
    metadata, redacted = collect_metadata(env, session_id, purpose)
    directory = env.paths.data_dir / session_id
    directory.mkdir(parents=True)
    for name, text in redacted.items():
        (directory / f"{name}.redacted.txt").write_text(f"# session: {session_id}\n{text}", encoding="utf-8")
    write_json(directory / "metadata.json", metadata)
    EventLog(directory / "events.jsonl", session_id, env.clock).append("session_start", purpose=purpose)
    return session_id


@dataclass
class Session:
    """An open session: its directory, metadata, and event log."""

    id: str
    directory: Path
    metadata: dict
    events: EventLog

    @property
    def offset_ms(self) -> float:
        """Container-minus-host clock offset measured at session start."""
        return self.metadata["clock_offset"]["offset_ms"]


def open_session(data_dir: Path, session_id: str, clock) -> Session:
    """Load a started session; an unknown id is refused so nothing is written outside a session."""
    directory = data_dir / session_id
    metadata_path = directory / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"session {session_id} has no metadata.json; run 'python -m harness start' first")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    return Session(session_id, directory, metadata, EventLog(directory / "events.jsonl", session_id, clock))
