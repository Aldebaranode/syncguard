"""JSON and JSON Lines helpers."""

from harness.jsonio import append_jsonl, read_json, read_jsonl, write_json


def test_json_round_trip_creates_parents(tmp_path):
    """write_json creates missing directories."""
    path = tmp_path / "a" / "b.json"
    write_json(path, {"session": "S4", "n": 1})
    assert read_json(path) == {"session": "S4", "n": 1}


def test_jsonl_appends_in_order_and_missing_reads_empty(tmp_path):
    """Rows come back in append order; a missing file is an empty list."""
    path = tmp_path / "rows.jsonl"
    assert read_jsonl(path) == []
    append_jsonl(path, {"i": 1})
    append_jsonl(path, {"i": 2})
    assert read_jsonl(path) == [{"i": 1}, {"i": 2}]
