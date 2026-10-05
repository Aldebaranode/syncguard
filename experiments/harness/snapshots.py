"""State, lock, and key-side file snapshots (R8, IV-11).

A key file contributes its address field, size, mtime, and sha256 only; the
private key is parsed in memory to read the address and never stored.
"""

import hashlib
import json
from pathlib import Path

from harness.jsonio import append_jsonl

KEY_SUFFIXES = ("", ".real", ".staged")


def file_stat(path: Path) -> dict:
    """Size, mtime in ms, and sha256 of an existing file."""
    info = path.stat()
    return {
        "size": info.st_size,
        "mtime_ms": info.st_mtime_ns // 1_000_000,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def key_file_fact(path: Path) -> dict:
    """Existence, size, mtime, sha256, and the address field of a key file."""
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        address = json.loads(path.read_text(encoding="utf-8")).get("address")
    except (json.JSONDecodeError, UnicodeDecodeError):
        address = None
    return {"path": str(path), "exists": True, **file_stat(path), "address": address}


def lock_file_fact(path: Path, pid_alive) -> dict:
    """Existence, size, mtime, PID content, and whether that PID runs."""
    if not path.exists():
        return {"path": str(path), "exists": False}
    content = path.read_text(encoding="utf-8").strip()
    pid = int(content) if content.isdigit() else None
    alive = pid_alive(pid) if pid is not None else False
    return {"path": str(path), "exists": True, **file_stat(path), "content": content, "pid": pid, "pid_alive": alive}


def state_fact(path: Path) -> dict:
    """Height, round, and step of priv_validator_state.json; the signature fields are not kept."""
    if not path.exists():
        return {"path": str(path), "exists": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "path": str(path),
        "exists": True,
        **file_stat(path),
        "height": int(data["height"]),
        "round": int(data["round"]),
        "step": int(data["step"]),
    }


def node_snapshot(label: str, key_path: Path, state_path: Path, pid_alive) -> dict:
    """Key-side files, the state file, and every *.lock beside the state file of one node."""
    keys = [key_file_fact(Path(f"{key_path}{suffix}")) for suffix in KEY_SUFFIXES]
    locks = [lock_file_fact(lock, pid_alive) for lock in sorted(state_path.parent.glob("*.lock"))]
    return {"node": label, "state": state_fact(state_path), "keys": keys, "locks": locks}


def process_alive(commands, pid: int) -> bool:
    """Whether `ps -p <pid>` finds the process."""
    return commands.run(["ps", "-p", str(pid), "-o", "pid="]).exit_code == 0


def take_snapshot(env, session, iteration: int, point: str) -> dict:
    """Record both nodes at one point of an iteration in snapshots.jsonl and log a snapshot event."""

    def pid_alive(pid: int) -> bool:
        """Liveness through the environment's command runner."""
        return process_alive(env.commands, pid)

    nodes = [
        node_snapshot("validator1", env.paths.primary_key, env.paths.primary_state, pid_alive),
        node_snapshot("validator2", env.paths.backup_key, env.paths.backup_state, pid_alive),
    ]
    snapshot = {"session": session.id, "iteration": iteration, "point": point, "t_ms": env.clock.now_ms(), "nodes": nodes}
    append_jsonl(session.directory / "snapshots.jsonl", snapshot)
    session.events.append("snapshot", iteration=iteration, point=point)
    return snapshot
