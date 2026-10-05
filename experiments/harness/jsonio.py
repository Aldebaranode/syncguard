"""JSON and JSON Lines helpers shared by every module that writes session files."""

import json
from pathlib import Path


def write_json(path: Path, data) -> None:
    """Write data as indented, key-sorted JSON, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path):
    """Read one JSON document."""
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, row: dict) -> None:
    """Append one row as a JSON line, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list:
    """Read every JSON line; a missing file reads as an empty list."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
