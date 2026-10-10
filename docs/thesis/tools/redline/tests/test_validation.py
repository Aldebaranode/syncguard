"""Tests for running the docx skill's validate.py on the output."""

import sys

from redline.validation import run_validate

FAKE_VALIDATE = """
import os, sys
print("args:", " ".join(sys.argv[1:]))
print("pythonpath:", os.environ.get("PYTHONPATH", ""))
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def _fake_script(tmp_path):
    """Write a stand-in validate.py that echoes its arguments."""
    script = tmp_path / "validate.py"
    script.write_text(FAKE_VALIDATE, encoding="utf-8")
    return script


def test_validate_runs_with_original_and_author_and_passes_on_exit_zero(tmp_path, monkeypatch):
    """The script gets OUT --original IN --author NAME; exit 0 means passed."""
    monkeypatch.setenv("REDLINE_VALIDATE_PYTHONPATH", "/opt/defusedxml-home")
    result = run_validate(tmp_path / "out.docx", tmp_path / "in.docx", "Syahid Nurrohim", script=_fake_script(tmp_path))
    assert result.ran is True
    assert result.passed is True
    assert result.exit_code == 0
    assert f"args: {tmp_path / 'out.docx'} --original {tmp_path / 'in.docx'} --author Syahid Nurrohim" in result.output
    assert "pythonpath: /opt/defusedxml-home" in result.output
    assert result.command.startswith(sys.executable)
    assert "--author 'Syahid Nurrohim'" in result.command


def test_validate_failure_is_reported(tmp_path, monkeypatch):
    """A non-zero exit is a failed validation, with the output kept."""
    monkeypatch.setenv("FAKE_EXIT", "1")
    result = run_validate(tmp_path / "out.docx", tmp_path / "in.docx", "A", script=_fake_script(tmp_path))
    assert (result.ran, result.passed, result.exit_code) == (True, False, 1)


def test_missing_script_is_reported_as_not_run(tmp_path):
    """Without the script there is no result to claim."""
    result = run_validate(tmp_path / "out.docx", tmp_path / "in.docx", "A", script=tmp_path / "absent.py")
    assert (result.ran, result.passed, result.exit_code) == (False, False, None)
    assert "not found" in result.output
